// mttr-app: the stateless sample application used in every trial.
//
// One binary, two modes:
//   - MODE=app (default): serves "/", optionally calling a dependency first.
//   - MODE=backend: the dependency; always answers 200.
//
// Fault switches (environment variables), so a single Git commit can turn a
// healthy release into a faulty one, reproducibly:
//   - ERROR_RATE   fraction of requests answered with 500 (e.g. 0.3)
//   - LATENCY_MS   extra delay added to every request
//   - CRASH_AFTER  exit(1) after this duration (e.g. 20s)
//
// Metrics are written in the Prometheus text format by hand, so the build needs
// no third-party modules and no network access.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math/rand/v2"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

var buckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5}

type metrics struct {
	mu       sync.Mutex
	requests map[string]uint64 // by status code
	depFails uint64
	hist     []uint64 // cumulative counts per bucket
	count    uint64
	sum      float64
}

func newMetrics() *metrics {
	return &metrics{requests: map[string]uint64{}, hist: make([]uint64, len(buckets))}
}

func (m *metrics) observe(code int, d time.Duration) {
	s := d.Seconds()
	m.mu.Lock()
	defer m.mu.Unlock()
	m.requests[strconv.Itoa(code)]++
	m.count++
	m.sum += s
	for i, b := range buckets {
		if s <= b {
			m.hist[i]++
		}
	}
}

func (m *metrics) write(w http.ResponseWriter, release string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprintln(w, "# HELP app_http_requests_total Requests served, by status code.")
	fmt.Fprintln(w, "# TYPE app_http_requests_total counter")
	codes := make([]string, 0, len(m.requests))
	for c := range m.requests {
		codes = append(codes, c)
	}
	sort.Strings(codes)
	for _, c := range codes {
		fmt.Fprintf(w, "app_http_requests_total{code=%q} %d\n", c, m.requests[c])
	}
	fmt.Fprintln(w, "# HELP app_dependency_failures_total Calls to the dependency that failed or timed out.")
	fmt.Fprintln(w, "# TYPE app_dependency_failures_total counter")
	fmt.Fprintf(w, "app_dependency_failures_total %d\n", m.depFails)
	fmt.Fprintln(w, "# HELP app_http_request_duration_seconds Request latency.")
	fmt.Fprintln(w, "# TYPE app_http_request_duration_seconds histogram")
	for i, b := range buckets {
		fmt.Fprintf(w, "app_http_request_duration_seconds_bucket{le=%q} %d\n", strconv.FormatFloat(b, 'f', -1, 64), m.hist[i])
	}
	fmt.Fprintf(w, "app_http_request_duration_seconds_bucket{le=\"+Inf\"} %d\n", m.count)
	fmt.Fprintf(w, "app_http_request_duration_seconds_sum %f\n", m.sum)
	fmt.Fprintf(w, "app_http_request_duration_seconds_count %d\n", m.count)
	fmt.Fprintln(w, "# HELP app_info Static information about the running release.")
	fmt.Fprintln(w, "# TYPE app_info gauge")
	fmt.Fprintf(w, "app_info{release=%q} 1\n", release)
}

func env(key, def string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return def
}

func envFloat(key string) float64 {
	v, err := strconv.ParseFloat(env(key, "0"), 64)
	if err != nil {
		log.Fatalf("%s: %v", key, err)
	}
	return v
}

func main() {
	mode := env("MODE", "app")
	release := env("RELEASE", "unknown")
	pod := env("POD_NAME", "unknown")
	errorRate := envFloat("ERROR_RATE")
	latency := time.Duration(envFloat("LATENCY_MS")) * time.Millisecond
	backendURL := env("BACKEND_URL", "")
	backendTimeout := time.Duration(envFloat("BACKEND_TIMEOUT_MS")) * time.Millisecond
	if backendTimeout == 0 {
		backendTimeout = 300 * time.Millisecond
	}
	client := &http.Client{Timeout: backendTimeout}
	m := newMetrics()

	if v := env("CRASH_AFTER", ""); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			log.Fatalf("CRASH_AFTER: %v", err)
		}
		go func() {
			time.Sleep(d)
			log.Printf("CRASH_AFTER=%s reached; exiting", v)
			os.Exit(1)
		}()
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		// Readiness deliberately ignores the dependency: a dependency fault
		// must show up as request errors, not as a pod leaving the endpoints.
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, _ *http.Request) { m.write(w, release) })
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		code := http.StatusOK
		defer func() { m.observe(code, time.Since(start)) }()
		w.Header().Set("X-Release", release)
		w.Header().Set("X-Pod", pod)
		if latency > 0 {
			time.Sleep(latency)
		}
		if mode == "app" && backendURL != "" {
			resp, err := client.Get(backendURL)
			if err == nil {
				resp.Body.Close()
			}
			if err != nil || resp.StatusCode != http.StatusOK {
				m.mu.Lock()
				m.depFails++
				m.mu.Unlock()
				code = http.StatusServiceUnavailable
				http.Error(w, "dependency unavailable", code)
				return
			}
		}
		if errorRate > 0 && rand.Float64() < errorRate {
			code = http.StatusInternalServerError
			http.Error(w, "injected error", code)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"mode": mode, "release": release, "pod": pod})
	})

	srv := &http.Server{Addr: ":" + env("PORT", "8080"), Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatal(err)
		}
	}()
	log.Printf("mttr-app mode=%s release=%s error_rate=%g latency=%s backend=%q",
		mode, release, errorRate, latency, strings.TrimSpace(backendURL))

	// On SIGTERM keep serving briefly, so the endpoint removal reaches the
	// router before the listener closes; otherwise every scale-down would
	// itself produce a burst of errors.
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)
	<-stop
	time.Sleep(3 * time.Second)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	srv.Shutdown(ctx)
}
