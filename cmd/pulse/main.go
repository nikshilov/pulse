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
	"sync"
	"syscall"
	"time"

	"github.com/nkkmnk/pulse/internal/claude"
	"github.com/nkkmnk/pulse/internal/config"
	"github.com/nkkmnk/pulse/internal/outbox"
	"github.com/nkkmnk/pulse/internal/prompt"
	"github.com/nkkmnk/pulse/internal/server"
	"github.com/nkkmnk/pulse/internal/store"
)

const (
	defaultAddr  = "127.0.0.1:3800"
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

	srv, err := server.New(server.Config{
		IPCSecret:    cfg.IPCSecret,
		Outbox:       ob,
		Builder:      builder,
		Claude:       cc,
		DefaultModel: defaultModel,
		Store:        s,
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
