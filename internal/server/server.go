package server

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/nkkmnk/pulse/internal/claude"
	"github.com/nkkmnk/pulse/internal/ingest"
	"github.com/nkkmnk/pulse/internal/outbox"
	"github.com/nkkmnk/pulse/internal/prompt"
	"github.com/nkkmnk/pulse/internal/retrieve"
	"github.com/nkkmnk/pulse/internal/store"
)

// ClaudeAPI is the subset of claude.Client we need. Allows fakes in tests.
type ClaudeAPI interface {
	Complete(ctx context.Context, req claude.CompleteRequest) (*claude.CompleteResponse, error)
}

// Config holds the dependencies a server needs.
type Config struct {
	IPCSecret    string
	Outbox       *outbox.Outbox
	Builder      *prompt.Builder
	Claude       ClaudeAPI
	DefaultModel string
	Store        *store.Store
	// Retrieval is the Phase G hybrid engine. Optional — when nil the
	// /retrieve endpoint is not registered.
	Retrieval *retrieve.Engine
}

// Server wraps the chi router.
type Server struct {
	cfg     Config
	started time.Time
}

func New(cfg Config) (*Server, error) {
	if cfg.IPCSecret == "" {
		return nil, errors.New("server: empty IPCSecret")
	}
	if cfg.Claude != nil && cfg.DefaultModel == "" {
		return nil, errors.New("server: Claude set but DefaultModel is empty")
	}
	return &Server{cfg: cfg, started: time.Now()}, nil
}

// Handler returns the root http.Handler with auth middleware.
func (s *Server) Handler() http.Handler {
	r := chi.NewRouter()
	r.Use(s.authMiddleware)
	r.Get("/health", s.handleHealth)
	r.Get("/outbox", s.handleOutboxList)
	r.Post("/outbox/ack", s.handleOutboxAck)
	r.Post("/msg", s.handleMsg)
	if s.cfg.Store != nil {
		r.Method(http.MethodPost, "/ingest", ingest.NewHandler(s.cfg.Store))
	}
	// /retrieve is always registered. handleRetrieve responds with 503 when
	// the engine is not configured (e.g. no Cohere API key) — better UX than
	// 404 since callers can tell intent vs absence.
	r.Post("/retrieve", s.handleRetrieve)
	return r
}

func (s *Server) authMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got := r.Header.Get("X-Pulse-Key")
		if subtle.ConstantTimeCompare([]byte(got), []byte(s.cfg.IPCSecret)) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

type healthResponse struct {
	Status        string  `json:"status"`
	UptimeSeconds float64 `json:"uptime_seconds"`
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	resp := healthResponse{
		Status:        "ok",
		UptimeSeconds: time.Since(s.started).Seconds(),
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}
