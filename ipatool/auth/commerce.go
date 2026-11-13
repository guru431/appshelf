package main

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/majd/ipatool/v2/internal/storeauth"
)

// Match the pinned upstream's commerce allowlist. Proofs are bound to the
// existing device identity and must never be returned for arbitrary URLs.
func commerceURL(endpoint, guid string) bool {
	u, err := url.ParseRequestURI(endpoint)
	if err != nil || u.Scheme != "https" || u.User != nil || strings.Contains(endpoint, "#") ||
		u.RawPath != "" || u.Opaque != "" || u.ForceQuery || u.Port() != "" && u.Port() != "443" {
		return false
	}
	q, err := url.ParseQuery(u.RawQuery)
	if err != nil || len(q) != 1 || len(q["guid"]) != 1 || q.Get("guid") != guid {
		return false
	}
	host := strings.ToLower(u.Hostname())
	if regexp.MustCompile(`^(?:p[0-9]+-)?buy\.itunes\.apple\.com$`).MatchString(host) {
		return u.Path == "/WebObjects/MZFinance.woa/wa/volumeStoreDownloadProduct"
	}
	if host != "downloaddispatch.itunes.apple.com" {
		return false
	}
	switch u.Path {
	case "/WebObjects/DownloadDispatch.woa/wa/ent/download",
		"/r/redownload", "/up/updateProduct":
		return true
	}
	return false
}

type commerceSigner func(context.Context, []byte, string, []byte) (map[string]string, []byte, error)

func commerceProof(ctx context.Context, state *deviceState, endpoint, guid string, sign commerceSigner) (map[string]string, error) {
	hardware, err := hex.DecodeString(guid)
	if err != nil || len(hardware) != 6 || !commerceURL(endpoint, guid) {
		return nil, errors.New("invalid commerce endpoint or device")
	}
	encoded, err := state.Get("anisette-v4")
	if err != nil {
		return nil, errState
	}
	defer clear(encoded)
	headers, next, err := sign(ctx, encoded, endpoint, hardware)
	if err != nil {
		return nil, errors.New("commerce device proof unavailable")
	}
	defer clear(next)
	if len(headers) != 2 || headers["X-Apple-AMD"] == "" || headers["X-Apple-AMD-M"] == "" {
		return nil, errors.New("empty commerce proof")
	}
	for key, value := range headers {
		if (key != "X-Apple-AMD" && key != "X-Apple-AMD-M") || len(value) > 8192 || strings.ContainsAny(value, "\r\n\x00") {
			return nil, errors.New("invalid commerce proof headers")
		}
	}
	if err = state.Set("anisette-v4", next); err != nil {
		return nil, err
	}
	return headers, nil
}

func printCommerceHeaders(cpp, endpoint string) {
	timer := time.AfterFunc(55*time.Second, func() { fail(1, "native_store_proof", "commerce device proof timed out") })
	defer timer.Stop()
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Second)
	defer cancel()
	output, err := exec.CommandContext(ctx, cpp, "--format", "json", "device-info").Output()
	var identity struct {
		GUID string `json:"guid"`
	}
	if err != nil || json.Unmarshal(output, &identity) != nil {
		fail(1, "bad_device_mac", "device identity unavailable")
	}
	home := os.Getenv("HOME")
	if !filepath.IsAbs(home) {
		fail(1, "usage", "absolute account HOME required")
	}
	state := &deviceState{path: filepath.Join(home, ".ipatool", "adi-state")}
	headers, err := commerceProof(ctx, state, endpoint, identity.GUID, storeauth.StoreAMDHeaders)
	if err != nil {
		fail(1, "native_store_proof", "commerce device proof unavailable; sign in again if device state is missing")
	}
	// Only the parent C++ client consumes this pipe. Never log the proof.
	_ = json.NewEncoder(os.Stdout).Encode(headers)
}
