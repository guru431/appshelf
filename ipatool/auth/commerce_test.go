package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestCommerceURLBoundToApplePathAndDevice(t *testing.T) {
	guid := "000393123456"
	good := []string{
		"https://buy.itunes.apple.com/WebObjects/MZFinance.woa/wa/volumeStoreDownloadProduct?guid=" + guid,
		"https://p42-buy.itunes.apple.com/WebObjects/MZFinance.woa/wa/volumeStoreDownloadProduct?guid=" + guid,
		"https://downloaddispatch.itunes.apple.com/WebObjects/DownloadDispatch.woa/wa/ent/download?guid=" + guid,
		"https://downloaddispatch.itunes.apple.com/r/redownload?guid=" + guid,
		"https://downloaddispatch.itunes.apple.com/up/updateProduct?guid=" + guid,
	}
	for _, endpoint := range good {
		if !commerceURL(endpoint, guid) {
			t.Fatal("rejected commerce endpoint", endpoint)
		}
	}
	bad := []string{
		good[0] + "&guid=" + guid, good[0] + "&token=secret", good[0] + "#fragment",
		"http://buy.itunes.apple.com/WebObjects/MZFinance.woa/wa/volumeStoreDownloadProduct?guid=" + guid,
		"https://buy.itunes.apple.com.evil.invalid/WebObjects/MZFinance.woa/wa/volumeStoreDownloadProduct?guid=" + guid,
		"https://user@buy.itunes.apple.com/WebObjects/MZFinance.woa/wa/volumeStoreDownloadProduct?guid=" + guid,
		"https://buy.itunes.apple.com:444/WebObjects/MZFinance.woa/wa/volumeStoreDownloadProduct?guid=" + guid,
		"https://buy.itunes.apple.com/other?guid=" + guid,
		"https://buy.itunes.apple.com/WebObjects/MZFinance.woa/wa/volumeStoreDownloadProduct?guid=other",
	}
	for _, endpoint := range bad {
		if commerceURL(endpoint, guid) {
			t.Fatal("accepted foreign endpoint", endpoint)
		}
	}
}

func TestCommerceProofPersistsDeviceStateBeforeReturningHeaders(t *testing.T) {
	state := &deviceState{path: filepath.Join(t.TempDir(), "adi-state")}
	if err := state.Set("anisette-v4", []byte("before")); err != nil {
		t.Fatal(err)
	}
	endpoint := "https://downloaddispatch.itunes.apple.com/r/redownload?guid=000393123456"
	sign := func(_ context.Context, old []byte, url string, hardware []byte) (map[string]string, []byte, error) {
		if string(old) != "before" || url != endpoint || len(hardware) != 6 {
			t.Fatal("wrong signing identity")
		}
		return map[string]string{"X-Apple-AMD": "proof", "X-Apple-AMD-M": "device"}, []byte("after"), nil
	}
	headers, err := commerceProof(context.Background(), state, endpoint, "000393123456", sign)
	if err != nil || headers["X-Apple-AMD"] != "proof" {
		t.Fatal("proof unavailable", err)
	}
	after, err := state.Get("anisette-v4")
	if err != nil || string(after) != "after" {
		t.Fatal("updated device state lost", err)
	}
	broken := &deviceState{path: filepath.Join(t.TempDir(), "missing")}
	if _, err = commerceProof(context.Background(), broken, endpoint, "000393123456", sign); err == nil {
		t.Fatal("missing state accepted")
	}
	if err = os.Remove(state.path); err != nil {
		t.Fatal(err)
	}
}

func TestCommerceProofRejectsUnsafeHeadersAndPreservesStateOnFailure(t *testing.T) {
	endpoint := "https://downloaddispatch.itunes.apple.com/r/redownload?guid=000393123456"
	for _, headers := range []map[string]string{nil, {"Cookie": "secret"}, {"X-Apple-AMD": "injected\r\nCookie: secret"}} {
		state := &deviceState{path: filepath.Join(t.TempDir(), "adi-state")}
		if err := state.Set("anisette-v4", []byte("before")); err != nil {
			t.Fatal(err)
		}
		sign := func(context.Context, []byte, string, []byte) (map[string]string, []byte, error) {
			return headers, []byte("after"), nil
		}
		if _, err := commerceProof(context.Background(), state, endpoint, "000393123456", sign); err == nil {
			t.Fatal("unsafe proof accepted")
		}
		after, _ := state.Get("anisette-v4")
		if string(after) != "before" {
			t.Fatal("state changed on failure")
		}
	}
	state := &deviceState{path: filepath.Join(t.TempDir(), "adi-state")}
	if err := state.Set("anisette-v4", []byte("before")); err != nil {
		t.Fatal(err)
	}
	sign := func(context.Context, []byte, string, []byte) (map[string]string, []byte, error) {
		return nil, nil, errors.New("private response")
	}
	if _, err := commerceProof(context.Background(), state, endpoint, "000393123456", sign); err == nil || err.Error() != "commerce device proof unavailable" {
		t.Fatal("unsafe error", err)
	}
}
