package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/nkkmnk/pulse/internal/claude"
	"github.com/nkkmnk/pulse/internal/config"
	"github.com/nkkmnk/pulse/internal/embed"
	"github.com/nkkmnk/pulse/internal/outbox"
	"github.com/nkkmnk/pulse/internal/prompt"
	"github.com/nkkmnk/pulse/internal/retrieve"
	"github.com/nkkmnk/pulse/internal/server"
	"github.com/nkkmnk/pulse/internal/store"
)

const (
	defaultAddr  = "127.0.0.1:18789"
	defaultModel = "claude-opus-4-6"
)

func main() {
	var (
		dataDir = flag.String("data-dir", filepath.Join(os.Getenv("HOME"), ".pulse"), "data directory")
		addr    = flag.String("addr", defaultAddr, "listen address")
	)
	flag.Parse()

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	if err := run(*dataDir, *addr); err != nil {
		slog.Error("startup failed", "error", err)
		os.Exit(1)
	}
}

func run(dataDir, addr string) error {
	cfg, err := config.Load(dataDir)
	if err != nil {
		return err
	}
	if cfg.AnthropicAPIKey == "" {
		return errors.New("ANTHROPIC_API_KEY env var is required")
	}

	s, err := store.Open(cfg.DBPath)
	if err != nil {
		return err
	}
	defer s.Close()

	ob := outbox.New(s.DB(), 30*time.Second)
	cc := claude.New(cfg.AnthropicAPIKey)

	soulPath := filepath.Join(dataDir, "soul.md")
	builder, err := prompt.NewBuilder(soulPath)
	if err != nil {
		return err
	}

	// Wire Phase G hybrid retrieval engine. Cohere key is optional — when
	// absent we leave Retrieval=nil so /retrieve returns 503 instead of 404.
	retrievalEngine, err := initRetrieval(s)
	if err != nil {
		// Log but don't fail startup — retrieval is opt-in.
		slog.Warn("retrieval init failed; /retrieve will return 503", "error", err)
	}

	srv, err := server.New(server.Config{
		IPCSecret:    cfg.IPCSecret,
		Outbox:       ob,
		Builder:      builder,
		Claude:       cc,
		DefaultModel: defaultModel,
		Store:        s,
		Retrieval:    retrievalEngine,
	})
	if err != nil {
		return err
	}

	httpSrv := &http.Server{
		Addr:              addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	// Background reaper for stale outbox leases.
	reaperCtx, cancelReaper := context.WithCancel(context.Background())

	var reaperWG sync.WaitGroup
	reaperWG.Add(1)
	go func() {
		defer reaperWG.Done()
		reaperLoop(reaperCtx, ob)
	}()

	slog.Info("pulse listening", "addr", addr, "data_dir", dataDir)

	// Graceful shutdown.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	errCh := make(chan error, 1)
	go func() {
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
	}()

	var runErr error
	select {
	case <-sigCh:
		slog.Info("shutdown signal received")
	case err := <-errCh:
		slog.Error("server error", "error", err)
		runErr = err
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if sErr := httpSrv.Shutdown(shutdownCtx); sErr != nil && runErr == nil {
		runErr = sErr
	}

	// Stop reaper BEFORE db.Close fires (via defer). Otherwise reaper may
	// error mid-Reap when the DB is closed under it.
	cancelReaper()
	reaperWG.Wait()

	return runErr
}

func reaperLoop(ctx context.Context, ob *outbox.Outbox) {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if n, err := ob.Reap(); err != nil {
				slog.Warn("reaper error", "error", err)
			} else if n > 0 {
				slog.Info("reaper requeued", "count", n)
			}
		}
	}
}

// initRetrieval wires the Phase G hybrid retrieval engine. Returns (nil, nil)
// when no Cohere API key is configured — that's not an error, just a signal
// that /retrieve should respond with 503. Returns (nil, err) only when the
// key IS present but the engine fails to load its indexes.
//
// Key resolution order:
//  1. COHERE_API_KEY env var
//  2. ~/.openclaw/secrets/cohere-key.txt
func initRetrieval(s *store.Store) (*retrieve.Engine, error) {
	apiKey := strings.TrimSpace(os.Getenv("COHERE_API_KEY"))
	if apiKey == "" {
		home, _ := os.UserHomeDir()
		keyPath := filepath.Join(home, ".openclaw", "secrets", "cohere-key.txt")
		if data, err := os.ReadFile(keyPath); err == nil {
			apiKey = strings.TrimSpace(string(data))
		}
	}
	if apiKey == "" {
		slog.Info("retrieval: no Cohere API key found (set COHERE_API_KEY or place in ~/.openclaw/secrets/cohere-key.txt); /retrieve will respond 503")
		return nil, nil
	}

	cohere := embed.NewCohere(apiKey, "", "")
	engine := retrieve.New(retrieve.Config{
		Store:    s,
		Embedder: cohere,
	})

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := engine.Init(ctx); err != nil {
		return nil, err
	}
	slog.Info("retrieval: engine initialized", "embedder", cohere.Model())
	return engine, nil
}
