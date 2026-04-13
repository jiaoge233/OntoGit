package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	_ "embed"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
	"time"
)

//go:embed frontend_dashboard.html
var dashboardHTML []byte

//go:embed frontend_login.html
var loginHTML []byte

type Config struct {
	Addr           string
	XiaoGuGitURL   string
	ProbabilityURL string
	ServiceAPIKey  string
	XGAuthSecret   string
	XGAuthUsername string
}

type HealthStatus struct {
	Name       string `json:"name"`
	URL        string `json:"url"`
	Status     string `json:"status"`
	StatusCode int    `json:"status_code,omitempty"`
	Detail     string `json:"detail,omitempty"`
}

type GatewayHealth struct {
	Service   string         `json:"service"`
	Status    string         `json:"status"`
	Timestamp string         `json:"timestamp"`
	Backends  []HealthStatus `json:"backends"`
}

type DashboardProject struct {
	ProjectID string `json:"project_id"`
}

type DashboardTimeline struct {
	Filename        string           `json:"filename"`
	VersionCount    int              `json:"version_count"`
	LatestVersionID any              `json:"latest_version_id"`
	History         []map[string]any `json:"history"`
	Extra           map[string]any   `json:"-"`
}

type DashboardProjectData struct {
	ProjectID    string           `json:"project_id"`
	Timelines    []map[string]any `json:"timelines"`
	CurrentFiles map[string]any   `json:"current_files"`
}

type DashboardSummary struct {
	Service   string                 `json:"service"`
	Status    string                 `json:"status"`
	Timestamp string                 `json:"timestamp"`
	Backends  []HealthStatus         `json:"backends"`
	Projects  []map[string]any       `json:"projects"`
	Data      []DashboardProjectData `json:"data"`
}

func main() {
	cfg := loadConfig()
	globalConfig = cfg

	xiaoGuGitURL, err := url.Parse(cfg.XiaoGuGitURL)
	if err != nil {
		log.Fatalf("invalid GATEWAY_XIAOGUGIT_URL: %v", err)
	}

	probabilityURL, err := url.Parse(cfg.ProbabilityURL)
	if err != nil {
		log.Fatalf("invalid GATEWAY_PROBABILITY_URL: %v", err)
	}

	xiaoGuGitProxy := newReverseProxy(xiaoGuGitURL)
	mux := http.NewServeMux()
	mux.HandleFunc("/health", healthHandler(cfg))
	mux.HandleFunc("/ui-dashboard", dashboardHandler())
	mux.HandleFunc("/login", loginHandler())
	mux.HandleFunc("/api/routes", routesHandler())
	mux.HandleFunc("/api/dashboard/summary", dashboardSummaryHandler(cfg))
	mux.Handle("/auth/", xiaoGuGitProxy)
	mux.Handle("/xg/", withStripPrefix("/xg", xiaoGuGitProxy))
	mux.Handle("/probability/", withStripPrefix("/probability", newReverseProxy(probabilityURL)))
	mux.Handle("/", rootOrXiaoGuGitHandler(cfg, xiaoGuGitProxy))

	server := &http.Server{
		Addr:              cfg.Addr,
		Handler:           logMiddleware(mux),
		ReadHeaderTimeout: 10 * time.Second,
	}

	log.Printf("gateway listening on %s", cfg.Addr)
	log.Printf("proxy xiaogugit -> %s", cfg.XiaoGuGitURL)
	log.Printf("proxy probability -> %s", cfg.ProbabilityURL)

	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("gateway server failed: %v", err)
	}
}

func loadConfig() Config {
	return Config{
		Addr:           getenv("GATEWAY_ADDR", ":8080"),
		XiaoGuGitURL:   strings.TrimRight(getenv("GATEWAY_XIAOGUGIT_URL", "http://127.0.0.1:8000"), "/"),
		ProbabilityURL: strings.TrimRight(getenv("GATEWAY_PROBABILITY_URL", "http://127.0.0.1:5000"), "/"),
		ServiceAPIKey:  strings.TrimSpace(os.Getenv("GATEWAY_SERVICE_API_KEY")),
		XGAuthSecret:   strings.TrimSpace(getenv("GATEWAY_XG_AUTH_SECRET", getenv("XG_AUTH_SECRET", "xiaogugit-auth-secret"))),
		XGAuthUsername: strings.TrimSpace(getenv("GATEWAY_XG_AUTH_USERNAME", getenv("XG_AUTH_USERNAME", "mogong"))),
	}
}

func getenv(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

func rootHandler(cfg Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"service": "data-infra-gateway",
			"status":  "running",
			"routes":  gatewayRouteCatalog(),
			"backends": map[string]string{
				"xiaogugit":   cfg.XiaoGuGitURL,
				"probability": cfg.ProbabilityURL,
			},
			"examples": map[string]string{
				"login":              "/login?next=/ui-dashboard",
				"dashboard_api":      "/api/dashboard/summary",
				"service_call":       "curl -H \"X-API-Key: <key>\" /api/dashboard/summary",
				"dashboard":          "/ui-dashboard",
				"xiaogugit_health":   "/xg/health",
				"probability_health": "/probability/health",
				"probability_reason": "/probability/api/llm/probability-reason",
			},
		})
	}
}

func routesHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"service": "data-infra-gateway",
			"routes":  gatewayRouteCatalog(),
			"count":   len(gatewayRouteCatalog()),
		})
	}
}

func rootOrXiaoGuGitHandler(cfg Config, xiaoGuGitProxy http.Handler) http.Handler {
	root := rootHandler(cfg)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			root.ServeHTTP(w, r)
			return
		}
		xiaoGuGitProxy.ServeHTTP(w, r)
	})
}

func dashboardHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(dashboardHTML)
	}
}

func loginHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(loginHTML)
	}
}

func dashboardSummaryHandler(cfg Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
		defer cancel()

		backends := []HealthStatus{
			checkBackend(ctx, "xiaogugit", cfg.XiaoGuGitURL+"/health"),
			checkBackend(ctx, "probability", cfg.ProbabilityURL+"/health"),
		}

		var projectPayload struct {
			Projects []map[string]any `json:"projects"`
		}
		if err := fetchJSON(ctx, cfg, http.MethodGet, cfg.XiaoGuGitURL+"/projects", r.Header, &projectPayload); err != nil {
			writeJSON(w, http.StatusBadGateway, map[string]any{
				"detail": "failed to load dashboard projects",
				"error":  err.Error(),
			})
			return
		}

		summary := DashboardSummary{
			Service:   "data-infra-gateway",
			Status:    "ok",
			Timestamp: time.Now().Format(time.RFC3339),
			Backends:  backends,
			Projects:  projectPayload.Projects,
			Data:      make([]DashboardProjectData, 0, len(projectPayload.Projects)),
		}

		for _, backend := range backends {
			if backend.Status != "ok" {
				summary.Status = "degraded"
				break
			}
		}

		for _, project := range projectPayload.Projects {
			projectID := strings.TrimSpace(fmt.Sprint(project["project_id"]))
			if projectID == "" {
				continue
			}

			var timelinePayload struct {
				Timelines []map[string]any `json:"timelines"`
			}
			if err := fetchJSON(ctx, cfg, http.MethodGet, cfg.XiaoGuGitURL+"/timelines/"+url.PathEscape(projectID), r.Header, &timelinePayload); err != nil {
				summary.Status = "degraded"
				summary.Data = append(summary.Data, DashboardProjectData{
					ProjectID:    projectID,
					Timelines:    []map[string]any{},
					CurrentFiles: map[string]any{"_error": err.Error()},
				})
				continue
			}

			currentFiles := map[string]any{}
			for _, timeline := range timelinePayload.Timelines {
				filename := strings.TrimSpace(fmt.Sprint(timeline["filename"]))
				if filename == "" {
					continue
				}

				var readPayload struct {
					Data any `json:"data"`
				}
				err := fetchJSON(ctx, cfg, http.MethodGet, cfg.XiaoGuGitURL+"/read/"+url.PathEscape(projectID)+"/"+url.PathEscape(filename), r.Header, &readPayload)
				if err != nil {
					currentFiles[filename] = map[string]any{"_error": err.Error()}
					continue
				}
				currentFiles[filename] = readPayload.Data
			}

			summary.Data = append(summary.Data, DashboardProjectData{
				ProjectID:    projectID,
				Timelines:    timelinePayload.Timelines,
				CurrentFiles: currentFiles,
			})
		}

		writeJSON(w, http.StatusOK, summary)
	}
}

func healthHandler(cfg Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()

		backends := []HealthStatus{
			checkBackend(ctx, "xiaogugit", cfg.XiaoGuGitURL+"/health"),
			checkBackend(ctx, "probability", cfg.ProbabilityURL+"/health"),
		}

		status := "ok"
		for _, backend := range backends {
			if backend.Status != "ok" {
				status = "degraded"
				break
			}
		}

		writeJSON(w, http.StatusOK, GatewayHealth{
			Service:   "data-infra-gateway",
			Status:    status,
			Timestamp: time.Now().Format(time.RFC3339),
			Backends:  backends,
		})
	}
}

func checkBackend(ctx context.Context, name, targetURL string) HealthStatus {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, targetURL, nil)
	if err != nil {
		return HealthStatus{Name: name, URL: targetURL, Status: "error", Detail: err.Error()}
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return HealthStatus{Name: name, URL: targetURL, Status: "error", Detail: err.Error()}
	}
	defer resp.Body.Close()

	status := "ok"
	if resp.StatusCode >= 400 {
		status = "error"
	}

	return HealthStatus{
		Name:       name,
		URL:        targetURL,
		Status:     status,
		StatusCode: resp.StatusCode,
	}
}

func fetchJSON(ctx context.Context, cfg Config, method, targetURL string, sourceHeaders http.Header, out any) error {
	req, err := http.NewRequestWithContext(ctx, method, targetURL, nil)
	if err != nil {
		return err
	}

	applyDownstreamAuth(cfg, sourceHeaders, req.Header)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return fmt.Errorf("backend returned HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	return json.NewDecoder(resp.Body).Decode(out)
}

func newReverseProxy(target *url.URL) *httputil.ReverseProxy {
	proxy := httputil.NewSingleHostReverseProxy(target)
	originalDirector := proxy.Director

	proxy.Director = func(req *http.Request) {
		sourceHeaders := req.Header.Clone()
		originalDirector(req)
		req.Host = target.Host
		req.Header.Set("X-Forwarded-Host", req.Host)
		req.Header.Set("X-Forwarded-Proto", "http")
		applyDownstreamAuth(globalConfig, sourceHeaders, req.Header)
	}

	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		writeJSON(w, http.StatusBadGateway, map[string]any{
			"detail": "gateway proxy error",
			"error":  err.Error(),
		})
	}

	return proxy
}

var globalConfig Config

func applyDownstreamAuth(cfg Config, sourceHeaders, targetHeaders http.Header) {
	if sourceHeaders == nil || targetHeaders == nil {
		return
	}

	if authorization := strings.TrimSpace(sourceHeaders.Get("Authorization")); authorization != "" {
		targetHeaders.Set("Authorization", authorization)
		return
	}

	if cookie := strings.TrimSpace(sourceHeaders.Get("Cookie")); cookie != "" {
		targetHeaders.Set("Cookie", cookie)
		return
	}

	if !serviceAPIKeyMatches(cfg, sourceHeaders) {
		return
	}

	if token := buildServiceAccessToken(cfg); token != "" {
		targetHeaders.Set("Authorization", "Bearer "+token)
	}
}

func serviceAPIKeyMatches(cfg Config, sourceHeaders http.Header) bool {
	if strings.TrimSpace(cfg.ServiceAPIKey) == "" || sourceHeaders == nil {
		return false
	}
	return hmac.Equal([]byte(strings.TrimSpace(sourceHeaders.Get("X-API-Key"))), []byte(cfg.ServiceAPIKey))
}

func buildServiceAccessToken(cfg Config) string {
	if strings.TrimSpace(cfg.XGAuthSecret) == "" || strings.TrimSpace(cfg.XGAuthUsername) == "" {
		return ""
	}

	payloadJSON, _ := json.Marshal(map[string]string{
		"username": cfg.XGAuthUsername,
	})
	payloadB64 := base64.RawURLEncoding.EncodeToString(payloadJSON)
	signature := hmac.New(sha256.New, []byte(cfg.XGAuthSecret))
	signature.Write([]byte(payloadB64))
	return payloadB64 + "." + fmt.Sprintf("%x", signature.Sum(nil))
}

func withStripPrefix(prefix string, next http.Handler) http.Handler {
	return http.StripPrefix(prefix, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "" || r.URL.Path == "/" {
			http.Redirect(w, r, prefix+"/health", http.StatusTemporaryRedirect)
			return
		}
		next.ServeHTTP(w, r)
	}))
}

func writeJSON(w http.ResponseWriter, statusCode int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(statusCode)
	_ = json.NewEncoder(w).Encode(payload)
}

func logMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		recorder := &statusRecorder{ResponseWriter: w, statusCode: http.StatusOK}
		next.ServeHTTP(recorder, r)
		log.Printf("%s %s -> %d (%s)", r.Method, r.URL.Path, recorder.statusCode, time.Since(start).Truncate(time.Millisecond))
	})
}

type statusRecorder struct {
	http.ResponseWriter
	statusCode int
}

func (r *statusRecorder) WriteHeader(statusCode int) {
	r.statusCode = statusCode
	r.ResponseWriter.WriteHeader(statusCode)
}
