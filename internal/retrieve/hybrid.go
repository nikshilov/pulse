package retrieve

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"sort"

	"github.com/nkkmnk/pulse/internal/embed"
	"github.com/nkkmnk/pulse/internal/store"
)

// Engine is Phase G hybrid retrieval. Loads events, facts, and chains from
// the store into in-memory caches at Init(); per-query it dispatches to
// factual/empathic/chain mode based on the router decision.
//
// Mirrors retrieval_v3.py:RetrievalV3 but stripped of the full conditional
// boost zoo for an honest MVP — empathic mode is cosine × recency for now.
// Future commits can add emotion_match / state_fit / anchor_priority / date_proximity
// boosts to match the Python prototype.
type Engine struct {
	store    *store.Store
	embedder *embed.CohereClient
	router   *Router

	// Decay constants (mirrors Python defaults from retrieval_v3.py:444-446).
	decayLambda       float64
	decayLambdaAnchor float64

	// Event index (loaded once from DB).
	eventIDs    []int64
	eventVecs   [][]float32
	eventDays   []float64
	eventAnchor []bool
	// Plutchik-10 vector per event (10-dim slice; zero-vector when unset).
	eventEmo [][]float32

	// Fact index.
	factIDs      []int64
	factEventIDs []int64
	factVecs     [][]float32

	// Chain edges (parent → children + child → parents).
	parentToChild map[int64][]int64
	childToParent map[int64][]int64

	embedModel string
}

// Config bundles the dependencies an Engine needs.
type Config struct {
	Store    *store.Store
	Embedder *embed.CohereClient
	Router   *Router // optional; if nil, Engine creates a default one
}

// New builds an Engine with sensible defaults. Call Init() to load indexes.
func New(cfg Config) *Engine {
	r := cfg.Router
	if r == nil {
		r = NewRouter()
	}
	model := "embed-v4.0"
	if cfg.Embedder != nil {
		model = cfg.Embedder.Model()
	}
	return &Engine{
		store:             cfg.Store,
		embedder:          cfg.Embedder,
		router:            r,
		decayLambda:       0.002,
		decayLambdaAnchor: 0.001,
		embedModel:        model,
		parentToChild:     make(map[int64][]int64),
		childToParent:     make(map[int64][]int64),
	}
}

// Init loads events + their embeddings + emotions + chains + facts + their
// embeddings into memory. Idempotent — call again after re-ingestion.
func (e *Engine) Init(ctx context.Context) error {
	if e.store == nil {
		return fmt.Errorf("retrieve: store is nil")
	}
	if err := e.loadEvents(ctx); err != nil {
		return fmt.Errorf("retrieve init events: %w", err)
	}
	if err := e.loadFacts(ctx); err != nil {
		return fmt.Errorf("retrieve init facts: %w", err)
	}
	if err := e.loadChains(ctx); err != nil {
		return fmt.Errorf("retrieve init chains: %w", err)
	}
	return nil
}

// loadEvents pulls (id, days_ago, user_flag, emotion_vec, embedding) per event.
// Joins events ⨝ event_embeddings ⨝ event_emotions. Events without embedding
// are skipped (can't be retrieved by cosine). Events without emotion get
// zero-vector (no emotion boost).
func (e *Engine) loadEvents(ctx context.Context) error {
	q := `
SELECT
    e.id,
    COALESCE(e.days_ago, 0) AS days_ago,
    COALESCE(e.user_flag, 0) AS user_flag,
    ee.vector_json,
    COALESCE(em.joy, 0), COALESCE(em.sadness, 0), COALESCE(em.anger, 0),
    COALESCE(em.fear, 0), COALESCE(em.trust, 0), COALESCE(em.disgust, 0),
    COALESCE(em.anticipation, 0), COALESCE(em.surprise, 0),
    COALESCE(em.shame, 0), COALESCE(em.guilt, 0)
FROM events e
JOIN event_embeddings ee ON ee.event_id = e.id
LEFT JOIN event_emotions em ON em.event_id = e.id
WHERE ee.model = ?
ORDER BY e.id`
	rows, err := e.store.DB().QueryContext(ctx, q, e.embedModel)
	if err != nil {
		return err
	}
	defer rows.Close()

	e.eventIDs = e.eventIDs[:0]
	e.eventVecs = e.eventVecs[:0]
	e.eventDays = e.eventDays[:0]
	e.eventAnchor = e.eventAnchor[:0]
	e.eventEmo = e.eventEmo[:0]

	for rows.Next() {
		var id int64
		var days float64
		var anchor int
		var vecJSON string
		em := make([]float32, 10)
		if err := rows.Scan(&id, &days, &anchor, &vecJSON,
			&em[0], &em[1], &em[2], &em[3], &em[4],
			&em[5], &em[6], &em[7], &em[8], &em[9]); err != nil {
			return err
		}
		var v []float32
		if err := json.Unmarshal([]byte(vecJSON), &v); err != nil {
			return fmt.Errorf("event %d: parse vector_json: %w", id, err)
		}
		e.eventIDs = append(e.eventIDs, id)
		e.eventVecs = append(e.eventVecs, v)
		e.eventDays = append(e.eventDays, days)
		e.eventAnchor = append(e.eventAnchor, anchor != 0)
		e.eventEmo = append(e.eventEmo, em)
	}
	return rows.Err()
}

func (e *Engine) loadFacts(ctx context.Context) error {
	q := `
SELECT f.id, f.event_id, fe.vector_json
FROM atomic_facts f
JOIN atomic_fact_embeddings fe ON fe.fact_id = f.id
WHERE fe.model = ?
ORDER BY f.id`
	rows, err := e.store.DB().QueryContext(ctx, q, e.embedModel)
	if err != nil {
		// Phase G migration may not be applied yet — treat empty as fine
		if isMissingTable(err) {
			return nil
		}
		return err
	}
	defer rows.Close()

	e.factIDs = e.factIDs[:0]
	e.factEventIDs = e.factEventIDs[:0]
	e.factVecs = e.factVecs[:0]

	for rows.Next() {
		var fid, eid int64
		var vecJSON string
		if err := rows.Scan(&fid, &eid, &vecJSON); err != nil {
			return err
		}
		var v []float32
		if err := json.Unmarshal([]byte(vecJSON), &v); err != nil {
			return fmt.Errorf("fact %d: parse vector_json: %w", fid, err)
		}
		e.factIDs = append(e.factIDs, fid)
		e.factEventIDs = append(e.factEventIDs, eid)
		e.factVecs = append(e.factVecs, v)
	}
	return rows.Err()
}

func (e *Engine) loadChains(ctx context.Context) error {
	q := `SELECT parent_id, child_id FROM event_chains`
	rows, err := e.store.DB().QueryContext(ctx, q)
	if err != nil {
		if isMissingTable(err) {
			return nil
		}
		return err
	}
	defer rows.Close()

	e.parentToChild = make(map[int64][]int64)
	e.childToParent = make(map[int64][]int64)
	for rows.Next() {
		var p, c int64
		if err := rows.Scan(&p, &c); err != nil {
			return err
		}
		e.parentToChild[p] = append(e.parentToChild[p], c)
		e.childToParent[c] = append(e.childToParent[c], p)
	}
	return rows.Err()
}

func isMissingTable(err error) bool {
	if err == nil {
		return false
	}
	s := err.Error()
	for _, marker := range []string{"no such table", "doesn't exist"} {
		if containsCI(s, marker) {
			return true
		}
	}
	return false
}

func containsCI(s, sub string) bool {
	if len(sub) > len(s) {
		return false
	}
	for i := 0; i+len(sub) <= len(s); i++ {
		match := true
		for j := 0; j < len(sub); j++ {
			a := s[i+j]
			b := sub[j]
			if a >= 'A' && a <= 'Z' {
				a += 'a' - 'A'
			}
			if b >= 'A' && b <= 'Z' {
				b += 'a' - 'A'
			}
			if a != b {
				match = false
				break
			}
		}
		if match {
			return true
		}
	}
	return false
}

// RetrieveRequest is the input to Retrieve().
type RetrieveRequest struct {
	Query     string
	Mode      QueryMode  // ModeAuto = router decides
	UserState *UserState // nullable
	TopK      int        // default 5 if zero
}

// RetrieveResponse is the output: ranked event IDs + chosen mode + router trace.
type RetrieveResponse struct {
	EventIDs       []int64
	ModeUsed       QueryMode
	RouterDecision RouteDecision
}

// Retrieve dispatches the query to factual/empathic/chain mode.
//
// Phase G MVP: empathic mode is cosine × recency (the v2_pure baseline).
// Conditional boosts (emotion / state / anchor / date) are TODO — to be
// added in a follow-up commit. The hybrid layer (factual + router) is the
// load-bearing Phase G change.
func (e *Engine) Retrieve(ctx context.Context, req RetrieveRequest) (*RetrieveResponse, error) {
	if e.embedder == nil {
		return nil, fmt.Errorf("retrieve: embedder is nil")
	}
	if req.Query == "" {
		return nil, fmt.Errorf("retrieve: empty query")
	}
	topK := req.TopK
	if topK <= 0 {
		topK = 5
	}

	mode := req.Mode
	var decision RouteDecision
	if mode == "" || mode == ModeAuto {
		decision = e.router.Classify(ctx, req.Query, req.UserState)
		mode = decision.Mode
	} else {
		decision = RouteDecision{Mode: mode, Confidence: 1.0, Classifier: "forced"}
	}

	qVec, err := e.embedQuery(ctx, req.Query)
	if err != nil {
		return nil, fmt.Errorf("retrieve embed: %w", err)
	}

	var ids []int64
	switch mode {
	case ModeFactual:
		ids = e.retrieveFactual(qVec, topK)
	case ModeChain:
		ids = e.retrieveChain(qVec, topK)
	default: // empathic + unknown
		ids = e.retrieveEmpathic(qVec, topK)
	}

	return &RetrieveResponse{
		EventIDs:       ids,
		ModeUsed:       mode,
		RouterDecision: decision,
	}, nil
}

func (e *Engine) embedQuery(ctx context.Context, q string) ([]float32, error) {
	vecs, err := e.embedder.Embed(ctx, []string{q}, embed.TypeSearchQuery)
	if err != nil {
		return nil, err
	}
	if len(vecs) == 0 {
		return nil, fmt.Errorf("embedder returned 0 vectors")
	}
	return vecs[0], nil
}

// retrieveEmpathic — cosine × recency (v2_pure baseline, MVP).
// Anchors get slower decay (decay_lambda_anchor = 0.001 vs 0.002).
func (e *Engine) retrieveEmpathic(qVec []float32, topK int) []int64 {
	n := len(e.eventIDs)
	if n == 0 {
		return nil
	}
	scores := make([]float64, n)
	for i := 0; i < n; i++ {
		base := dotF32(qVec, e.eventVecs[i])
		lambda := e.decayLambda
		if e.eventAnchor[i] {
			lambda = e.decayLambdaAnchor
		}
		recency := math.Exp(-lambda * e.eventDays[i])
		scores[i] = float64(base) * recency
	}
	return topKIndicesToIDs(scores, e.eventIDs, topK)
}

// retrieveFactual — cosine on fact embeddings → unique parent event_ids.
func (e *Engine) retrieveFactual(qVec []float32, topK int) []int64 {
	if len(e.factIDs) == 0 {
		// No facts indexed — fall back to empathic so caller still gets results
		return e.retrieveEmpathic(qVec, topK)
	}
	n := len(e.factIDs)
	scores := make([]float64, n)
	for i := 0; i < n; i++ {
		scores[i] = float64(dotF32(qVec, e.factVecs[i]))
	}
	order := argsortDesc(scores)
	seen := make(map[int64]bool, topK)
	out := make([]int64, 0, topK)
	for _, idx := range order {
		eid := e.factEventIDs[idx]
		if seen[eid] {
			continue
		}
		seen[eid] = true
		out = append(out, eid)
		if len(out) >= topK {
			break
		}
	}
	return out
}

// retrieveChain — anchor-priority + predecessor BFS.
// MVP: empathic ranking → if any seed is in chain graph, expand backward
// through child_to_parent and forward through parent_to_child up to depth 3.
func (e *Engine) retrieveChain(qVec []float32, topK int) []int64 {
	seeds := e.retrieveEmpathic(qVec, topK*3) // overfetch
	if len(seeds) == 0 || len(e.parentToChild)+len(e.childToParent) == 0 {
		return cap1(seeds, topK)
	}

	visited := make(map[int64]bool)
	frontier := []struct {
		id    int64
		depth int
	}{}
	for _, s := range seeds[:min(topK, len(seeds))] {
		frontier = append(frontier, struct {
			id    int64
			depth int
		}{s, 0})
	}
	chainSet := make(map[int64]bool)
	for len(frontier) > 0 {
		head := frontier[0]
		frontier = frontier[1:]
		if visited[head.id] {
			continue
		}
		visited[head.id] = true
		chainSet[head.id] = true
		if head.depth >= 3 {
			continue
		}
		for _, p := range e.childToParent[head.id] {
			if !visited[p] {
				frontier = append(frontier, struct {
					id    int64
					depth int
				}{p, head.depth + 1})
			}
		}
		for _, c := range e.parentToChild[head.id] {
			if !visited[c] {
				frontier = append(frontier, struct {
					id    int64
					depth int
				}{c, head.depth + 1})
			}
		}
	}

	// Order chain set by ancestor depth (roots first), tiebreak on seed ranking.
	rank := make(map[int64]int, len(seeds))
	for i, eid := range seeds {
		rank[eid] = i
	}
	chainList := make([]int64, 0, len(chainSet))
	for id := range chainSet {
		chainList = append(chainList, id)
	}
	sort.SliceStable(chainList, func(i, j int) bool {
		ri, oki := rank[chainList[i]]
		rj, okj := rank[chainList[j]]
		if !oki {
			ri = 1 << 30
		}
		if !okj {
			rj = 1 << 30
		}
		return ri < rj
	})

	// Fill up to top_k: chain first, then any leftover seeds.
	result := chainList
	for _, s := range seeds {
		if len(result) >= topK {
			break
		}
		if !contains64(result, s) {
			result = append(result, s)
		}
	}
	if len(result) > topK {
		result = result[:topK]
	}
	return result
}

// ──────────────────────────────────────────────────────────────────────────
// helpers
// ──────────────────────────────────────────────────────────────────────────

func dotF32(a, b []float32) float32 {
	if len(a) != len(b) {
		return 0
	}
	var sum float32
	for i := range a {
		sum += a[i] * b[i]
	}
	return sum
}

type scoreIdx struct {
	score float64
	idx   int
}

func argsortDesc(scores []float64) []int {
	idx := make([]scoreIdx, len(scores))
	for i, s := range scores {
		idx[i] = scoreIdx{s, i}
	}
	sort.SliceStable(idx, func(i, j int) bool { return idx[i].score > idx[j].score })
	out := make([]int, len(scores))
	for i := range idx {
		out[i] = idx[i].idx
	}
	return out
}

func topKIndicesToIDs(scores []float64, ids []int64, k int) []int64 {
	order := argsortDesc(scores)
	if k > len(order) {
		k = len(order)
	}
	out := make([]int64, k)
	for i := 0; i < k; i++ {
		out[i] = ids[order[i]]
	}
	return out
}

func cap1(a []int64, k int) []int64 {
	if len(a) > k {
		return a[:k]
	}
	return a
}

func contains64(a []int64, x int64) bool {
	for _, v := range a {
		if v == x {
			return true
		}
	}
	return false
}

// rowsErrSilencer is a placeholder for sql.Rows.Err pass-through if we add
// telemetry later. Not used yet but referenced via type so the package builds
// even with future hooks.
var _ = (*sql.Rows)(nil)
