package server

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/nkkmnk/pulse/internal/claude"
	"github.com/nkkmnk/pulse/internal/outbox"
	"github.com/nkkmnk/pulse/internal/prompt"
)

type outboxRow struct {
	ID       int64  `json:"id"`
	ChatID   int64  `json:"chat_id"`
	Text     string `json:"text"`
	ReplyTo  *int64 `json:"reply_to,omitempty"`
	Media    string `json:"media,omitempty"`
	Priority string `json:"priority"`
	Attempts int    `json:"attempts"`
}

func (s *Server) handleOutboxList(w http.ResponseWriter, r *http.Request) {
	limit := 10
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 100 {
			limit = n
		}
	}
	records, err := s.cfg.Outbox.Claim(limit)
	if err != nil {
		slog.Error("outbox claim failed", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	out := make([]outboxRow, 0, len(records))
	for _, rec := range records {
		row := outboxRow{
			ID:       rec.ID,
			ChatID:   rec.ChatID,
			Text:     rec.Text,
			Media:    rec.Media,
			Priority: rec.Priority,
			Attempts: rec.Attempts,
		}
		if rec.ReplyTo != nil {
			rt := *rec.ReplyTo
			row.ReplyTo = &rt
		}
		out = append(out, row)
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(out)
}

type ackRequest struct {
	ID      int64  `json:"id"`
	Success bool   `json:"success"`
	Error   string `json:"error,omitempty"`
}

func (s *Server) handleOutboxAck(w http.ResponseWriter, r *http.Request) {
	var req ackRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if req.ID == 0 {
		http.Error(w, "missing id", http.StatusBadRequest)
		return
	}
	if err := s.cfg.Outbox.Ack(req.ID, req.Success, req.Error); err != nil {
		slog.Error("outbox ack failed", "err", err, "id", req.ID)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

type msgRequest struct {
	ChatID    int64  `json:"chat_id"`
	MessageID int64  `json:"message_id"`
	Text      string `json:"text"`
	// ReplyTo and Timestamp are sent by the bridge; reserved for M2 when
	// thread context and time-aware prompts land.
	ReplyTo   *int64 `json:"reply_to,omitempty"`
	Timestamp string `json:"timestamp"`
}

func (s *Server) handleMsg(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20) // 1 MiB

	var req msgRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if req.ChatID == 0 || req.Text == "" {
		http.Error(w, "chat_id and text required", http.StatusBadRequest)
		return
	}

	built, err := s.cfg.Builder.Build(prompt.BuildInput{UserMessage: req.Text})
	if err != nil {
		slog.Error("prompt build failed", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 120*time.Second)
	defer cancel()
	resp, err := s.cfg.Claude.Complete(ctx, claude.CompleteRequest{
		Model:    s.cfg.DefaultModel,
		System:   built.System,
		Messages: built.Messages,
	})
	if err != nil {
		slog.Error("claude call failed", "err", err)
		http.Error(w, "upstream error", http.StatusBadGateway)
		return
	}

	if strings.TrimSpace(resp.Text) == "" {
		slog.Warn("claude returned empty text", "chat_id", req.ChatID, "stop_reason", resp.StopReason)
		http.Error(w, "empty reply", http.StatusBadGateway)
		return
	}

	msgID := req.MessageID
	if _, err := s.cfg.Outbox.Enqueue(outbox.Message{
		ChatID:  req.ChatID,
		Text:    resp.Text,
		ReplyTo: &msgID,
	}); err != nil {
		slog.Error("outbox enqueue failed", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusAccepted)
}
