package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	_ "embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	_ "github.com/go-sql-driver/mysql"
)

//go:embed frontend_dashboard.html
var dashboardHTML []byte

//go:embed frontend_login.html
var loginHTML []byte

//go:embed frontend_agent.html
var agentHTML []byte

//go:embed frontend_users.html
var usersHTML []byte

//go:embed frontend_api_docs.html
var apiDocsHTML []byte

type Config struct {
	Env            string
	Addr           string
	XiaoGuGitURL   string
	ProbabilityURL string
	ServiceAPIKey  string
	XGAuthSecret   string
	XGAuthUsername string
	XGAuthCookie   string
	AgentDir       string
	MySQLDSN       string
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

type AgentQueryRequest struct {
	Question   string `json:"question"`
	ProjectID  string `json:"project_id"`
	Filename   string `json:"filename"`
	IncludeRaw bool   `json:"include_raw"`
	AuthAPIKey string `json:"-"`
}

type AuthPrincipal struct {
	Kind     string
	UserID   int64
	Username string
	APIKeyID int64
}

type RegisterRequest struct {
	Username    string `json:"username"`
	Password    string `json:"password"`
	DisplayName string `json:"display_name"`
}

type LoginRequest struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

type CreateAPIKeyRequest struct {
	Name string `json:"name"`
}

type StarRequest struct {
	ProjectID string `json:"project_id"`
	Filename  string `json:"filename"`
	VersionID int64  `json:"version_id"`
}

func main() {
	cfg := loadConfig()
	globalConfig = cfg
	store, err := initUserStore(cfg)
	if err != nil {
		log.Fatalf("gateway user store init failed: %v", err)
	}
	userStore = store
	if store == nil {
		log.Printf("gateway mysql user store disabled")
	} else {
		log.Printf("gateway mysql user store enabled")
	}

	xiaoGuGitURL, err := url.Parse(cfg.XiaoGuGitURL)
	if err != nil {
		log.Fatalf("invalid GATEWAY_XIAOGUGIT_URL: %v", err)
	}

	probabilityURL, err := url.Parse(cfg.ProbabilityURL)
	if err != nil {
		log.Fatalf("invalid GATEWAY_PROBABILITY_URL: %v", err)
	}

	xiaoGuGitProxy := newReverseProxy(xiaoGuGitURL)
	probabilityProxy := withStripPrefix("/probability", newReverseProxy(probabilityURL))
	mux := http.NewServeMux()
	mux.HandleFunc("/health", healthHandler(cfg))
	mux.HandleFunc("/ui-dashboard", protectedHTMLHandler(cfg, dashboardHTML))
	mux.HandleFunc("/ui-agent", protectedHTMLHandler(cfg, agentHTML))
	mux.HandleFunc("/ui-users", publicHTMLHandler(usersHTML))
	mux.HandleFunc("/ui-api-docs", protectedHTMLHandler(cfg, apiDocsHTML))
	mux.HandleFunc("/docs", docsRedirectHandler())
	mux.HandleFunc("/login", loginHandler())
	mux.HandleFunc("/api/routes", routesHandler())
	mux.HandleFunc("/api/users/register", userRegisterHandler(cfg))
	mux.HandleFunc("/api/users/login", userLoginHandler(cfg))
	mux.HandleFunc("/api/users/logout", userLogoutHandler(cfg))
	mux.Handle("/api/users/api-keys", requireUserAuth(cfg, userAPIKeysHandler(cfg)))
	mux.Handle("/api/users/api-keys/", requireUserAuth(cfg, userAPIKeyItemHandler(cfg)))
	mux.Handle("/api/stars/star", requireGatewayAuth(cfg, versionStarHandler(cfg, true)))
	mux.Handle("/api/stars/unstar", requireGatewayAuth(cfg, versionStarHandler(cfg, false)))
	mux.Handle("/api/dashboard/summary", requireGatewayAuth(cfg, dashboardSummaryHandler(cfg)))
	mux.Handle("/api/agent/query", requireGatewayAuth(cfg, agentQueryHandler(cfg)))
	mux.Handle("/auth/", xiaoGuGitProxy)
	mux.Handle("/xg/", requireGatewayAuth(cfg, withStripPrefix("/xg", xiaoGuGitProxy)))
	mux.Handle("/probability/", allowPublicHealth("/probability", probabilityProxy, requireGatewayAuth(cfg, probabilityProxy)))
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
	values := buildMergedEnv()
	return Config{
		Env:            normalizeEnv(getValue(values, "GATEWAY_ENV", "development")),
		Addr:           getValue(values, "GATEWAY_ADDR", ":8080"),
		XiaoGuGitURL:   strings.TrimRight(getValue(values, "GATEWAY_XIAOGUGIT_URL", "http://127.0.0.1:8000"), "/"),
		ProbabilityURL: strings.TrimRight(getValue(values, "GATEWAY_PROBABILITY_URL", "http://127.0.0.1:5000"), "/"),
		ServiceAPIKey:  strings.TrimSpace(values["GATEWAY_SERVICE_API_KEY"]),
		XGAuthSecret:   strings.TrimSpace(getValue(values, "GATEWAY_XG_AUTH_SECRET", getValue(values, "XG_AUTH_SECRET", "xiaogugit-auth-secret"))),
		XGAuthUsername: strings.TrimSpace(getValue(values, "GATEWAY_XG_AUTH_USERNAME", getValue(values, "XG_AUTH_USERNAME", "mogong"))),
		XGAuthCookie:   strings.TrimSpace(getValue(values, "GATEWAY_XG_AUTH_COOKIE_NAME", getValue(values, "XG_AUTH_COOKIE_NAME", "xg_session"))),
		AgentDir:       strings.TrimSpace(values["GATEWAY_AGENT_DIR"]),
		MySQLDSN:       strings.TrimSpace(values["GATEWAY_MYSQL_DSN"]),
	}
}

func getenv(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

func buildMergedEnv() map[string]string {
	values := map[string]string{}
	baseEnv := readEnvFile(filepath.Join(".", ".env"))
	mode := normalizeEnv(firstNonEmpty(os.Getenv("GATEWAY_ENV"), baseEnv["GATEWAY_ENV"]))
	for key, value := range baseEnv {
		values[key] = value
	}
	for key, value := range readEnvFile(filepath.Join(".", ".env."+mode)) {
		values[key] = value
	}
	for key, value := range readEnvFile(filepath.Join(".", ".env.local")) {
		values[key] = value
	}
	for _, entry := range os.Environ() {
		key, value, ok := strings.Cut(entry, "=")
		if ok {
			values[key] = value
		}
	}
	values["GATEWAY_ENV"] = normalizeEnv(values["GATEWAY_ENV"])
	return values
}

func readEnvFile(path string) map[string]string {
	values := map[string]string{}
	content, err := os.ReadFile(path)
	if err != nil {
		return values
	}
	for _, rawLine := range strings.Split(string(content), "\n") {
		line := strings.TrimSpace(rawLine)
		if line == "" || strings.HasPrefix(line, "#") || !strings.Contains(line, "=") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		value = strings.Trim(strings.TrimSpace(value), `"'`)
		if key != "" {
			values[key] = value
		}
	}
	return values
}

func getValue(values map[string]string, key, fallback string) string {
	value := strings.TrimSpace(values[key])
	if value == "" {
		return fallback
	}
	return value
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

func normalizeEnv(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "prod", "production":
		return "production"
	case "dev", "development", "":
		return "development"
	default:
		return "development"
	}
}

var userStore *UserStore

type UserStore struct {
	db *sql.DB
}

func initUserStore(cfg Config) (*UserStore, error) {
	if strings.TrimSpace(cfg.MySQLDSN) == "" {
		return nil, nil
	}
	db, err := sql.Open("mysql", cfg.MySQLDSN)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(5)
	db.SetConnMaxLifetime(30 * time.Minute)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}

	store := &UserStore{db: db}
	if err := store.migrate(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return store, nil
}

func (s *UserStore) migrate(ctx context.Context) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS gateway_users (
			id BIGINT PRIMARY KEY AUTO_INCREMENT,
			username VARCHAR(64) NOT NULL UNIQUE,
			display_name VARCHAR(128) NOT NULL DEFAULT '',
			password_hash VARCHAR(255) NOT NULL,
			status VARCHAR(32) NOT NULL DEFAULT 'active',
			created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
			updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci`,
		`CREATE TABLE IF NOT EXISTS gateway_api_keys (
			id BIGINT PRIMARY KEY AUTO_INCREMENT,
			user_id BIGINT NOT NULL,
			name VARCHAR(128) NOT NULL DEFAULT '',
			key_prefix VARCHAR(32) NOT NULL,
			key_hash CHAR(64) NOT NULL UNIQUE,
			status VARCHAR(32) NOT NULL DEFAULT 'active',
			last_used_at DATETIME(6) NULL,
			created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
			revoked_at DATETIME(6) NULL,
			INDEX idx_gateway_api_keys_user_id (user_id),
			INDEX idx_gateway_api_keys_status (status),
			CONSTRAINT fk_gateway_api_keys_user FOREIGN KEY (user_id) REFERENCES gateway_users(id) ON DELETE CASCADE
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci`,
		`CREATE TABLE IF NOT EXISTS gateway_version_stars (
			id BIGINT PRIMARY KEY AUTO_INCREMENT,
			project_id VARCHAR(128) NOT NULL,
			filename VARCHAR(255) NOT NULL,
			version_id BIGINT NOT NULL,
			user_id BIGINT NULL,
			api_key_id BIGINT NULL,
			voter_key VARCHAR(160) NOT NULL,
			status VARCHAR(32) NOT NULL DEFAULT 'active',
			created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
			updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
			UNIQUE KEY uniq_gateway_version_stars_vote (project_id, filename, version_id, voter_key),
			INDEX idx_gateway_version_stars_version (project_id, filename, version_id, status),
			INDEX idx_gateway_version_stars_voter (voter_key)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci`,
	}
	for _, statement := range statements {
		if _, err := s.db.ExecContext(ctx, statement); err != nil {
			return err
		}
	}
	return nil
}

func (s *UserStore) createUser(ctx context.Context, req RegisterRequest) (map[string]any, string, error) {
	username := normalizeUsername(req.Username)
	if username == "" {
		return nil, "", errors.New("username is required")
	}
	if len(req.Password) < 6 {
		return nil, "", errors.New("password must be at least 6 characters")
	}
	passwordHash, err := hashPassword(req.Password)
	if err != nil {
		return nil, "", err
	}
	displayName := strings.TrimSpace(req.DisplayName)
	if displayName == "" {
		displayName = username
	}

	result, err := s.db.ExecContext(
		ctx,
		`INSERT INTO gateway_users (username, display_name, password_hash, status) VALUES (?, ?, ?, 'active')`,
		username,
		displayName,
		passwordHash,
	)
	if err != nil {
		return nil, "", err
	}
	userID, err := result.LastInsertId()
	if err != nil {
		return nil, "", err
	}
	apiKey, apiKeyRow, err := s.createAPIKey(ctx, userID, "default")
	if err != nil {
		return nil, "", err
	}
	return map[string]any{
		"id":           userID,
		"username":     username,
		"display_name": displayName,
		"api_key":      apiKeyRow,
	}, apiKey, nil
}

func (s *UserStore) login(ctx context.Context, req LoginRequest) (int64, string, error) {
	username := normalizeUsername(req.Username)
	if username == "" || req.Password == "" {
		return 0, "", errors.New("username and password are required")
	}
	var userID int64
	var passwordHash, status string
	err := s.db.QueryRowContext(
		ctx,
		`SELECT id, password_hash, status FROM gateway_users WHERE username = ?`,
		username,
	).Scan(&userID, &passwordHash, &status)
	if err != nil {
		return 0, "", err
	}
	if status != "active" || !verifyPassword(req.Password, passwordHash) {
		return 0, "", errors.New("invalid credentials")
	}
	return userID, username, nil
}

func (s *UserStore) createAPIKey(ctx context.Context, userID int64, name string) (string, map[string]any, error) {
	rawKey, err := generateAPIKey()
	if err != nil {
		return "", nil, err
	}
	name = strings.TrimSpace(name)
	if name == "" {
		name = "default"
	}
	prefix := keyPrefix(rawKey)
	result, err := s.db.ExecContext(
		ctx,
		`INSERT INTO gateway_api_keys (user_id, name, key_prefix, key_hash, status) VALUES (?, ?, ?, ?, 'active')`,
		userID,
		name,
		prefix,
		hashAPIKey(rawKey),
	)
	if err != nil {
		return "", nil, err
	}
	apiKeyID, err := result.LastInsertId()
	if err != nil {
		return "", nil, err
	}
	return rawKey, map[string]any{
		"id":         apiKeyID,
		"name":       name,
		"key_prefix": prefix,
		"status":     "active",
	}, nil
}

func (s *UserStore) listAPIKeys(ctx context.Context, userID int64) ([]map[string]any, error) {
	rows, err := s.db.QueryContext(
		ctx,
		`SELECT id, name, key_prefix, status, created_at, last_used_at, revoked_at
		 FROM gateway_api_keys WHERE user_id = ? ORDER BY id DESC`,
		userID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	items := []map[string]any{}
	for rows.Next() {
		var id int64
		var name, prefix, status string
		var createdAt time.Time
		var lastUsedAt, revokedAt sql.NullTime
		if err := rows.Scan(&id, &name, &prefix, &status, &createdAt, &lastUsedAt, &revokedAt); err != nil {
			return nil, err
		}
		item := map[string]any{
			"id":         id,
			"name":       name,
			"key_prefix": prefix,
			"status":     status,
			"created_at": createdAt.Format(time.RFC3339),
		}
		if lastUsedAt.Valid {
			item["last_used_at"] = lastUsedAt.Time.Format(time.RFC3339)
		}
		if revokedAt.Valid {
			item["revoked_at"] = revokedAt.Time.Format(time.RFC3339)
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *UserStore) revokeAPIKey(ctx context.Context, userID, apiKeyID int64) (bool, error) {
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_api_keys SET status = 'revoked', revoked_at = CURRENT_TIMESTAMP(6)
		 WHERE id = ? AND user_id = ? AND status = 'active'`,
		apiKeyID,
		userID,
	)
	if err != nil {
		return false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return false, err
	}
	return affected > 0, nil
}

func (s *UserStore) authenticateAPIKey(ctx context.Context, apiKey string) (*AuthPrincipal, error) {
	apiKey = strings.TrimSpace(apiKey)
	if apiKey == "" {
		return nil, sql.ErrNoRows
	}
	var principal AuthPrincipal
	var status string
	err := s.db.QueryRowContext(
		ctx,
		`SELECT k.id, k.user_id, u.username, k.status
		 FROM gateway_api_keys k
		 JOIN gateway_users u ON u.id = k.user_id
		 WHERE k.key_hash = ? AND u.status = 'active'`,
		hashAPIKey(apiKey),
	).Scan(&principal.APIKeyID, &principal.UserID, &principal.Username, &status)
	if err != nil {
		return nil, err
	}
	if status != "active" {
		return nil, sql.ErrNoRows
	}
	_, _ = s.db.ExecContext(ctx, `UPDATE gateway_api_keys SET last_used_at = CURRENT_TIMESTAMP(6) WHERE id = ?`, principal.APIKeyID)
	principal.Kind = "api_key"
	return &principal, nil
}

func (s *UserStore) setVersionStar(ctx context.Context, req StarRequest, principal *AuthPrincipal, voterKey string, active bool) (bool, string, error) {
	if voterKey == "" {
		return false, "", errors.New("missing voter identity")
	}
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return false, "", err
	}
	defer tx.Rollback()

	if active {
		result, err := tx.ExecContext(
			ctx,
			`INSERT IGNORE INTO gateway_version_stars
				(project_id, filename, version_id, user_id, api_key_id, voter_key, status)
			 VALUES (?, ?, ?, ?, ?, ?, 'active')`,
			req.ProjectID,
			req.Filename,
			req.VersionID,
			nullableInt64(principalUserID(principal)),
			nullableInt64(principalAPIKeyID(principal)),
			voterKey,
		)
		if err != nil {
			return false, "", err
		}
		affected, err := result.RowsAffected()
		if err != nil {
			return false, "", err
		}
		if affected > 0 {
			if err := tx.Commit(); err != nil {
				return false, "", err
			}
			return true, "starred", nil
		}

		var status string
		err = tx.QueryRowContext(
			ctx,
			`SELECT status FROM gateway_version_stars
			 WHERE project_id = ? AND filename = ? AND version_id = ? AND voter_key = ?
			 FOR UPDATE`,
			req.ProjectID,
			req.Filename,
			req.VersionID,
			voterKey,
		).Scan(&status)
		if err != nil {
			return false, "", err
		}
		if status == "active" {
			if err := tx.Commit(); err != nil {
				return false, "", err
			}
			return false, "already_starred", nil
		}
		if _, err := tx.ExecContext(
			ctx,
			`UPDATE gateway_version_stars
			 SET status = 'active', user_id = ?, api_key_id = ?
			 WHERE project_id = ? AND filename = ? AND version_id = ? AND voter_key = ?`,
			nullableInt64(principalUserID(principal)),
			nullableInt64(principalAPIKeyID(principal)),
			req.ProjectID,
			req.Filename,
			req.VersionID,
			voterKey,
		); err != nil {
			return false, "", err
		}
		if err := tx.Commit(); err != nil {
			return false, "", err
		}
		return true, "starred", nil
	}

	var status string
	err = tx.QueryRowContext(
		ctx,
		`SELECT status FROM gateway_version_stars
		 WHERE project_id = ? AND filename = ? AND version_id = ? AND voter_key = ?
		 FOR UPDATE`,
		req.ProjectID,
		req.Filename,
		req.VersionID,
		voterKey,
	).Scan(&status)
	if errors.Is(err, sql.ErrNoRows) {
		if err := tx.Commit(); err != nil {
			return false, "", err
		}
		return false, "already_unstarred", nil
	}
	if err != nil {
		return false, "", err
	}
	if status != "active" {
		if err := tx.Commit(); err != nil {
			return false, "", err
		}
		return false, "already_unstarred", nil
	}
	if _, err := tx.ExecContext(
		ctx,
		`UPDATE gateway_version_stars
		 SET status = 'revoked', user_id = ?, api_key_id = ?
		 WHERE project_id = ? AND filename = ? AND version_id = ? AND voter_key = ?`,
		nullableInt64(principalUserID(principal)),
		nullableInt64(principalAPIKeyID(principal)),
		req.ProjectID,
		req.Filename,
		req.VersionID,
		voterKey,
	); err != nil {
		return false, "", err
	}
	if err := tx.Commit(); err != nil {
		return false, "", err
	}
	return true, "unstarred", nil
}

func (s *UserStore) forceVersionStarStatus(ctx context.Context, req StarRequest, voterKey, status string) error {
	if voterKey == "" || status == "" {
		return nil
	}
	_, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_version_stars
		 SET status = ?
		 WHERE project_id = ? AND filename = ? AND version_id = ? AND voter_key = ?`,
		status,
		req.ProjectID,
		req.Filename,
		req.VersionID,
		voterKey,
	)
	return err
}

func normalizeUsername(value string) string {
	return strings.ToLower(strings.TrimSpace(value))
}

func generateAPIKey() (string, error) {
	randomBytes := make([]byte, 24)
	if _, err := rand.Read(randomBytes); err != nil {
		return "", err
	}
	return "xgk_" + hex.EncodeToString(randomBytes), nil
}

func keyPrefix(apiKey string) string {
	apiKey = strings.TrimSpace(apiKey)
	if len(apiKey) <= 12 {
		return apiKey
	}
	return apiKey[:12]
}

func hashAPIKey(apiKey string) string {
	sum := sha256.Sum256([]byte(strings.TrimSpace(apiKey)))
	return hex.EncodeToString(sum[:])
}

func nullableInt64(value int64) any {
	if value <= 0 {
		return nil
	}
	return value
}

func principalUserID(principal *AuthPrincipal) int64 {
	if principal == nil {
		return 0
	}
	return principal.UserID
}

func principalAPIKeyID(principal *AuthPrincipal) int64 {
	if principal == nil {
		return 0
	}
	return principal.APIKeyID
}

func hashPassword(password string) (string, error) {
	salt := make([]byte, 16)
	if _, err := rand.Read(salt); err != nil {
		return "", err
	}
	saltHex := hex.EncodeToString(salt)
	digest := passwordDigest(password, saltHex)
	return "sha256$" + saltHex + "$" + digest, nil
}

func verifyPassword(password, stored string) bool {
	parts := strings.Split(stored, "$")
	if len(parts) != 3 || parts[0] != "sha256" {
		return false
	}
	expected := passwordDigest(password, parts[1])
	return hmac.Equal([]byte(expected), []byte(parts[2]))
}

func passwordDigest(password, saltHex string) string {
	mac := hmac.New(sha256.New, []byte(saltHex))
	mac.Write([]byte(password))
	return hex.EncodeToString(mac.Sum(nil))
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
				"agent_api":          "/api/agent/query",
				"service_call":       "curl -H \"X-API-Key: <key>\" /api/dashboard/summary",
				"dashboard":          "/ui-dashboard",
				"agent":              "/ui-agent",
				"users":              "/ui-users",
				"api_docs":           "/ui-api-docs",
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
	protectedProxy := requireGatewayAuth(cfg, xiaoGuGitProxy)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			root.ServeHTTP(w, r)
			return
		}
		if isBrowserPagePath(r.URL.Path) && authenticateGatewayRequest(cfg, r) == nil {
			redirectToLogin(w, r)
			return
		}
		if isBrowserPagePath(r.URL.Path) {
			xiaoGuGitProxy.ServeHTTP(w, r)
			return
		}
		protectedProxy.ServeHTTP(w, r)
	})
}

func protectedHTMLHandler(cfg Config, html []byte) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if authenticateGatewayRequest(cfg, r) == nil {
			redirectToLogin(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(html)
	}
}

func publicHTMLHandler(html []byte) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(html)
	}
}

func redirectToLogin(w http.ResponseWriter, r *http.Request) {
	next := r.URL.RequestURI()
	if next == "" {
		next = "/ui-dashboard"
	}
	http.Redirect(w, r, "/login?next="+url.QueryEscape(next), http.StatusFound)
}

func isBrowserPagePath(path string) bool {
	switch path {
	case "/ui", "/ui-visual", "/ui-modern", "/ui-visual-modern", "/docs", "/redoc":
		return true
	default:
		return false
	}
}

func docsRedirectHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/ui-api-docs", http.StatusFound)
	}
}

func loginHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(loginHTML)
	}
}

func userRegisterHandler(cfg Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"detail": "method not allowed"})
			return
		}
		if userStore == nil {
			writeJSON(w, http.StatusServiceUnavailable, map[string]any{"detail": safeErrorDetail})
			return
		}
		var payload RegisterRequest
		if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&payload); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"detail": safeErrorDetail})
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		user, apiKey, err := userStore.createUser(ctx, payload)
		if err != nil {
			log.Printf("user register failed: %v", err)
			writeJSON(w, http.StatusBadRequest, map[string]any{"detail": safeErrorDetail})
			return
		}
		token := buildUserAccessToken(cfg, user["id"].(int64), fmt.Sprint(user["username"]))
		setGatewaySessionCookie(w, cfg, buildServiceAccessToken(cfg))
		writeJSON(w, http.StatusCreated, map[string]any{
			"status":          "success",
			"user":            user,
			"access_token":    token,
			"xg_access_token": buildServiceAccessToken(cfg),
			"api_key":         apiKey,
			"api_key_note":    "API Key is only shown once. Store it securely.",
		})
	}
}

func userLoginHandler(cfg Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"detail": "method not allowed"})
			return
		}
		if userStore == nil {
			writeJSON(w, http.StatusServiceUnavailable, map[string]any{"detail": safeErrorDetail})
			return
		}
		var payload LoginRequest
		if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&payload); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"detail": safeErrorDetail})
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		userID, username, err := userStore.login(ctx, payload)
		if err != nil {
			log.Printf("user login failed for %s: %v", normalizeUsername(payload.Username), err)
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "Unauthorized"})
			return
		}
		setGatewaySessionCookie(w, cfg, buildServiceAccessToken(cfg))
		writeJSON(w, http.StatusOK, map[string]any{
			"status":          "success",
			"access_token":    buildUserAccessToken(cfg, userID, username),
			"xg_access_token": buildServiceAccessToken(cfg),
			"user": map[string]any{
				"id":       userID,
				"username": username,
			},
		})
	}
}

func userLogoutHandler(cfg Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"detail": "method not allowed"})
			return
		}
		clearGatewaySessionCookie(w, cfg)
		writeJSON(w, http.StatusOK, map[string]any{"status": "success"})
	}
}

func userAPIKeysHandler(cfg Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		principal := principalFromContext(r.Context())
		if principal == nil || principal.UserID <= 0 {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "Unauthorized"})
			return
		}
		if userStore == nil {
			writeJSON(w, http.StatusServiceUnavailable, map[string]any{"detail": safeErrorDetail})
			return
		}

		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		switch r.Method {
		case http.MethodGet:
			keys, err := userStore.listAPIKeys(ctx, principal.UserID)
			if err != nil {
				log.Printf("api key list failed user_id=%d: %v", principal.UserID, err)
				writeJSON(w, http.StatusBadGateway, map[string]any{"detail": safeErrorDetail})
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"api_keys": keys})
		case http.MethodPost:
			var payload CreateAPIKeyRequest
			if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&payload); err != nil && !errors.Is(err, io.EOF) {
				writeJSON(w, http.StatusBadRequest, map[string]any{"detail": safeErrorDetail})
				return
			}
			apiKey, row, err := userStore.createAPIKey(ctx, principal.UserID, payload.Name)
			if err != nil {
				log.Printf("api key create failed user_id=%d: %v", principal.UserID, err)
				writeJSON(w, http.StatusBadGateway, map[string]any{"detail": safeErrorDetail})
				return
			}
			writeJSON(w, http.StatusCreated, map[string]any{
				"status":       "success",
				"api_key":      apiKey,
				"api_key_info": row,
				"api_key_note": "API Key is only shown once. Store it securely.",
			})
		default:
			writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"detail": "method not allowed"})
		}
	}
}

func userAPIKeyItemHandler(cfg Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		principal := principalFromContext(r.Context())
		if principal == nil || principal.UserID <= 0 {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "Unauthorized"})
			return
		}
		if userStore == nil {
			writeJSON(w, http.StatusServiceUnavailable, map[string]any{"detail": safeErrorDetail})
			return
		}
		if r.Method != http.MethodDelete {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"detail": "method not allowed"})
			return
		}
		idText := strings.TrimPrefix(r.URL.Path, "/api/users/api-keys/")
		apiKeyID, err := strconv.ParseInt(strings.Trim(idText, "/"), 10, 64)
		if err != nil || apiKeyID <= 0 {
			writeJSON(w, http.StatusBadRequest, map[string]any{"detail": "invalid api key id"})
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		revoked, err := userStore.revokeAPIKey(ctx, principal.UserID, apiKeyID)
		if err != nil {
			log.Printf("api key revoke failed user_id=%d api_key_id=%d: %v", principal.UserID, apiKeyID, err)
			writeJSON(w, http.StatusBadGateway, map[string]any{"detail": safeErrorDetail})
			return
		}
		if !revoked {
			writeJSON(w, http.StatusNotFound, map[string]any{"detail": "not found"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"status": "success", "revoked_api_key_id": apiKeyID})
	}
}

func versionStarHandler(cfg Config, active bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"detail": "method not allowed"})
			return
		}
		if userStore == nil {
			writeJSON(w, http.StatusServiceUnavailable, map[string]any{"detail": safeErrorDetail})
			return
		}

		var payload StarRequest
		if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&payload); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"detail": safeErrorDetail})
			return
		}
		payload.ProjectID = strings.TrimSpace(payload.ProjectID)
		payload.Filename = strings.TrimSpace(payload.Filename)
		if payload.ProjectID == "" || payload.Filename == "" || payload.VersionID <= 0 {
			writeJSON(w, http.StatusBadRequest, map[string]any{"detail": "project_id, filename and version_id are required"})
			return
		}

		principal := principalFromContext(r.Context())
		voterKey := voterKeyFromRequest(principal, r)
		if voterKey == "" {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "Unauthorized"})
			return
		}

		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()
		changed, action, err := userStore.setVersionStar(ctx, payload, principal, voterKey, active)
		if err != nil {
			log.Printf("gateway star vote failed project_id=%s filename=%s version_id=%d active=%t: %v", payload.ProjectID, payload.Filename, payload.VersionID, active, err)
			writeJSON(w, http.StatusBadGateway, map[string]any{"detail": safeErrorDetail})
			return
		}

		var backendResult map[string]any
		if changed {
			targetPath := "/version-star"
			targetPayload := map[string]any{
				"project_id": payload.ProjectID,
				"filename":   payload.Filename,
				"version_id": payload.VersionID,
				"increment":  1,
			}
			if !active {
				targetPath = "/version-unstar"
				targetPayload = map[string]any{
					"project_id": payload.ProjectID,
					"filename":   payload.Filename,
					"version_id": payload.VersionID,
					"decrement":  1,
				}
			}
			if err := postJSON(ctx, cfg, cfg.XiaoGuGitURL+targetPath, r.Header, targetPayload, &backendResult); err != nil {
				compensatingStatus := "revoked"
				if !active {
					compensatingStatus = "active"
				}
				if compensateErr := userStore.forceVersionStarStatus(context.Background(), payload, voterKey, compensatingStatus); compensateErr != nil {
					log.Printf("gateway star compensation failed project_id=%s filename=%s version_id=%d action=%s: %v", payload.ProjectID, payload.Filename, payload.VersionID, action, compensateErr)
				}
				log.Printf("gateway star backend sync failed project_id=%s filename=%s version_id=%d action=%s: %v", payload.ProjectID, payload.Filename, payload.VersionID, action, err)
				writeJSON(w, http.StatusBadGateway, map[string]any{"detail": safeErrorDetail})
				return
			}
		} else {
			detailURL := cfg.XiaoGuGitURL + "/version-detail/" + url.PathEscape(payload.ProjectID) + "/" + url.PathEscape(strconv.FormatInt(payload.VersionID, 10)) + "?filename=" + url.QueryEscape(payload.Filename)
			if err := fetchJSON(ctx, cfg, http.MethodGet, detailURL, r.Header, &backendResult); err != nil {
				log.Printf("gateway star detail refresh failed project_id=%s filename=%s version_id=%d action=%s: %v", payload.ProjectID, payload.Filename, payload.VersionID, action, err)
				backendResult = map[string]any{}
			}
		}

		stars := extractStars(backendResult)
		writeJSON(w, http.StatusOK, map[string]any{
			"status":     "success",
			"action":     action,
			"changed":    changed,
			"project_id": payload.ProjectID,
			"filename":   payload.Filename,
			"version_id": payload.VersionID,
			"stars":      stars,
			"backend":    backendResult,
		})
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
			log.Printf("dashboard projects load failed: %v", err)
			writeJSON(w, http.StatusBadGateway, map[string]any{
				"detail": safeErrorDetail,
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
				log.Printf("dashboard timeline load failed for project %s: %v", projectID, err)
				summary.Status = "degraded"
				summary.Data = append(summary.Data, DashboardProjectData{
					ProjectID:    projectID,
					Timelines:    []map[string]any{},
					CurrentFiles: map[string]any{"_error": safeErrorDetail},
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
					log.Printf("dashboard file load failed for %s/%s: %v", projectID, filename, err)
					currentFiles[filename] = map[string]any{"_error": safeErrorDetail}
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

func agentQueryHandler(cfg Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]any{
				"detail": "method not allowed",
			})
			return
		}

		var payload AgentQueryRequest
		if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&payload); err != nil {
			log.Printf("agent query invalid request body: %v", err)
			writeJSON(w, http.StatusBadRequest, map[string]any{
				"detail": safeErrorDetail,
			})
			return
		}

		payload.Question = strings.TrimSpace(payload.Question)
		payload.ProjectID = strings.TrimSpace(payload.ProjectID)
		payload.Filename = strings.TrimSpace(payload.Filename)
		payload.AuthAPIKey = requestAPIKey(r.Header)
		if payload.Question == "" {
			writeJSON(w, http.StatusBadRequest, map[string]any{
				"detail": "question is required",
			})
			return
		}

		result, err := runAgentQuery(r.Context(), cfg, payload)
		if err != nil {
			log.Printf("agent query failed: %v", err)
			writeJSON(w, http.StatusBadGateway, map[string]any{
				"detail": safeErrorDetail,
			})
			return
		}

		writeJSON(w, http.StatusOK, result)
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

func runAgentQuery(ctx context.Context, cfg Config, payload AgentQueryRequest) (map[string]any, error) {
	baseDir, err := executableDir()
	if err != nil {
		return nil, err
	}

	agentDir, err := resolveAgentDir(baseDir, cfg.AgentDir)
	if err != nil {
		return nil, err
	}
	scriptPath := filepath.Join(agentDir, "run_git_query_agent.py")
	if _, err := os.Stat(scriptPath); err != nil {
		return nil, fmt.Errorf("agent script not found: %w", err)
	}

	runCtx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()

	args := []string{
		scriptPath,
		payload.Question,
		"--base-url", publicGatewayBaseURL(cfg),
	}
	if payload.ProjectID != "" {
		args = append(args, "--project-id", payload.ProjectID)
	}
	if payload.Filename != "" {
		args = append(args, "--filename", payload.Filename)
	}
	if strings.TrimSpace(cfg.ServiceAPIKey) != "" {
		args = append(args, "--api-key", cfg.ServiceAPIKey)
	} else if strings.TrimSpace(payload.AuthAPIKey) != "" {
		args = append(args, "--api-key", payload.AuthAPIKey)
	}
	if payload.IncludeRaw {
		args = append(args, "--include-raw")
	}

	cmd := execCommandContext(runCtx, "python", args...)
	cmd.Dir = agentDir
	cmd.Env = append(os.Environ(),
		"PYTHONIOENCODING=utf-8",
		"PYTHONUTF8=1",
	)
	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		errText := strings.TrimSpace(stderr.String())
		if errText == "" {
			errText = err.Error()
		}
		return nil, fmt.Errorf("%s", errText)
	}

	var result map[string]any
	if err := json.Unmarshal([]byte(stdout.String()), &result); err != nil {
		return nil, fmt.Errorf("invalid agent output: %w", err)
	}
	return result, nil
}

func resolveAgentDir(baseDir, configuredDir string) (string, error) {
	candidates := []string{}
	if configuredDir != "" {
		candidates = append(candidates, configuredDir)
	}
	candidates = append(candidates,
		filepath.Join(baseDir, "agent"),
		filepath.Join(baseDir, "..", "agent"),
	)
	for _, candidate := range candidates {
		if candidate == "" {
			continue
		}
		absCandidate, err := filepath.Abs(candidate)
		if err != nil {
			continue
		}
		if info, err := os.Stat(filepath.Join(absCandidate, "run_git_query_agent.py")); err == nil && !info.IsDir() {
			return absCandidate, nil
		}
	}
	return "", fmt.Errorf("agent directory not found")
}

var execCommandContext = func(ctx context.Context, name string, args ...string) *exec.Cmd {
	return exec.CommandContext(ctx, name, args...)
}

func executableDir() (string, error) {
	executablePath, err := os.Executable()
	if err != nil {
		return "", err
	}
	return filepath.Dir(executablePath), nil
}

func publicGatewayBaseURL(cfg Config) string {
	addr := strings.TrimSpace(cfg.Addr)
	if addr == "" {
		addr = ":8080"
	}
	if strings.HasPrefix(addr, ":") {
		return "http://127.0.0.1" + addr
	}
	if strings.HasPrefix(addr, "http://") || strings.HasPrefix(addr, "https://") {
		return strings.TrimRight(addr, "/")
	}
	return "http://" + strings.TrimRight(addr, "/")
}

func checkBackend(ctx context.Context, name, targetURL string) HealthStatus {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, targetURL, nil)
	if err != nil {
		log.Printf("health request build failed for %s: %v", name, err)
		return HealthStatus{Name: name, URL: targetURL, Status: "error", Detail: safeErrorDetail}
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Printf("health request failed for %s: %v", name, err)
		return HealthStatus{Name: name, URL: targetURL, Status: "error", Detail: safeErrorDetail}
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

func postJSON(ctx context.Context, cfg Config, targetURL string, sourceHeaders http.Header, payload any, out any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, targetURL, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
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
	if out == nil {
		return nil
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
		log.Printf("gateway proxy error for %s %s: %v", r.Method, r.URL.Path, err)
		writeJSON(w, http.StatusBadGateway, map[string]any{
			"detail": safeErrorDetail,
		})
	}

	return proxy
}

var globalConfig Config

const safeErrorDetail = "请稍后重试"

type principalContextKey struct{}

func principalFromContext(ctx context.Context) *AuthPrincipal {
	if ctx == nil {
		return nil
	}
	principal, _ := ctx.Value(principalContextKey{}).(*AuthPrincipal)
	return principal
}

func requestAPIKey(headers http.Header) string {
	if headers == nil {
		return ""
	}
	apiKey := strings.TrimSpace(headers.Get("X-API-Key"))
	if apiKey == "" {
		apiKey = strings.TrimSpace(headers.Get("apikey"))
	}
	if apiKey == "" {
		authorization := strings.TrimSpace(headers.Get("Authorization"))
		if strings.HasPrefix(strings.ToLower(authorization), "apikey ") {
			apiKey = strings.TrimSpace(authorization[len("apikey "):])
		}
	}
	return apiKey
}

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
	return authenticateServiceAPIKey(context.Background(), cfg, sourceHeaders) != nil
}

func authenticateServiceAPIKey(ctx context.Context, cfg Config, sourceHeaders http.Header) *AuthPrincipal {
	apiKey := requestAPIKey(sourceHeaders)
	if apiKey == "" {
		return nil
	}
	if strings.TrimSpace(cfg.ServiceAPIKey) != "" && hmac.Equal([]byte(apiKey), []byte(cfg.ServiceAPIKey)) {
		return &AuthPrincipal{Kind: "static_api_key"}
	}
	if userStore == nil {
		return nil
	}
	lookupCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	principal, err := userStore.authenticateAPIKey(lookupCtx, apiKey)
	if err != nil {
		return nil
	}
	return principal
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

func buildUserAccessToken(cfg Config, userID int64, username string) string {
	if strings.TrimSpace(cfg.XGAuthSecret) == "" || userID <= 0 {
		return ""
	}
	payloadJSON, _ := json.Marshal(map[string]any{
		"type":     "gateway_user",
		"user_id":  userID,
		"username": strings.TrimSpace(username),
		"exp":      time.Now().Add(24 * time.Hour).Unix(),
	})
	payloadB64 := base64.RawURLEncoding.EncodeToString(payloadJSON)
	signature := hmac.New(sha256.New, []byte(cfg.XGAuthSecret))
	signature.Write([]byte(payloadB64))
	return payloadB64 + "." + fmt.Sprintf("%x", signature.Sum(nil))
}

func setGatewaySessionCookie(w http.ResponseWriter, cfg Config, token string) {
	if w == nil || strings.TrimSpace(cfg.XGAuthCookie) == "" || strings.TrimSpace(token) == "" {
		return
	}
	http.SetCookie(w, &http.Cookie{
		Name:     cfg.XGAuthCookie,
		Value:    token,
		Path:     "/",
		MaxAge:   int((24 * time.Hour).Seconds()),
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
	})
}

func clearGatewaySessionCookie(w http.ResponseWriter, cfg Config) {
	if w == nil || strings.TrimSpace(cfg.XGAuthCookie) == "" {
		return
	}
	http.SetCookie(w, &http.Cookie{
		Name:     cfg.XGAuthCookie,
		Value:    "",
		Path:     "/",
		MaxAge:   -1,
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
	})
}

func authenticateUserBearer(cfg Config, authorization string) *AuthPrincipal {
	authorization = strings.TrimSpace(authorization)
	if !strings.HasPrefix(strings.ToLower(authorization), "bearer ") {
		return nil
	}
	token := strings.TrimSpace(authorization[len("bearer "):])
	payload, ok := verifySignedToken(cfg, token)
	if !ok {
		return nil
	}
	if fmt.Sprint(payload["type"]) != "gateway_user" {
		return nil
	}
	userID, ok := numericClaim(payload["user_id"])
	if !ok || userID <= 0 {
		return nil
	}
	exp, ok := numericClaim(payload["exp"])
	if ok && exp > 0 && time.Now().Unix() > exp {
		return nil
	}
	return &AuthPrincipal{
		Kind:     "user_bearer",
		UserID:   userID,
		Username: strings.TrimSpace(fmt.Sprint(payload["username"])),
	}
}

func verifySignedToken(cfg Config, token string) (map[string]any, bool) {
	token = strings.TrimSpace(token)
	if token == "" || !strings.Contains(token, ".") || strings.TrimSpace(cfg.XGAuthSecret) == "" {
		return nil, false
	}
	payloadB64, signatureHex, ok := strings.Cut(token, ".")
	if !ok || payloadB64 == "" || signatureHex == "" {
		return nil, false
	}

	signature := hmac.New(sha256.New, []byte(cfg.XGAuthSecret))
	signature.Write([]byte(payloadB64))
	expectedSignature := fmt.Sprintf("%x", signature.Sum(nil))
	if !hmac.Equal([]byte(signatureHex), []byte(expectedSignature)) {
		return nil, false
	}

	payloadRaw, err := base64.RawURLEncoding.DecodeString(payloadB64)
	if err != nil {
		return nil, false
	}
	var payload map[string]any
	if err := json.Unmarshal(payloadRaw, &payload); err != nil {
		return nil, false
	}
	return payload, true
}

func numericClaim(value any) (int64, bool) {
	switch typed := value.(type) {
	case float64:
		return int64(typed), true
	case int64:
		return typed, true
	case int:
		return int64(typed), true
	case json.Number:
		result, err := typed.Int64()
		return result, err == nil
	case string:
		result, err := strconv.ParseInt(strings.TrimSpace(typed), 10, 64)
		return result, err == nil
	default:
		return 0, false
	}
}

func extractStars(payload map[string]any) int64 {
	if payload == nil {
		return 0
	}
	candidates := []any{
		payload["stars"],
		payload["community_score"],
	}
	if version, ok := payload["version"].(map[string]any); ok {
		candidates = append(candidates, version["stars"], version["community_score"])
	}
	if backend, ok := payload["backend"].(map[string]any); ok {
		candidates = append(candidates, backend["stars"], backend["community_score"])
	}
	for _, candidate := range candidates {
		if value, ok := numericClaim(candidate); ok {
			return value
		}
	}
	return 0
}

func requireGatewayAuth(cfg Config, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if principal := authenticateGatewayRequest(cfg, r); principal != nil {
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), principalContextKey{}, principal)))
			return
		}
		writeJSON(w, http.StatusUnauthorized, map[string]any{
			"detail": "Unauthorized",
		})
	})
}

func requireUserAuth(cfg Config, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		principal := authenticateUserRequest(cfg, r)
		if principal != nil && principal.UserID > 0 {
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), principalContextKey{}, principal)))
			return
		}
		writeJSON(w, http.StatusUnauthorized, map[string]any{
			"detail": "Unauthorized",
		})
	})
}

func allowPublicHealth(prefix string, publicNext, protectedNext http.Handler) http.Handler {
	healthPath := strings.TrimRight(prefix, "/") + "/health"
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == healthPath {
			publicNext.ServeHTTP(w, r)
			return
		}
		protectedNext.ServeHTTP(w, r)
	})
}

func gatewayRequestAuthenticated(cfg Config, r *http.Request) bool {
	return authenticateGatewayRequest(cfg, r) != nil
}

func authenticateGatewayRequest(cfg Config, r *http.Request) *AuthPrincipal {
	if r == nil {
		return nil
	}
	if principal := authenticateServiceAPIKey(r.Context(), cfg, r.Header); principal != nil {
		return principal
	}
	if principal := authenticateUserBearer(cfg, r.Header.Get("Authorization")); principal != nil {
		return principal
	}
	if gatewayBearerAuthenticated(cfg, r.Header.Get("Authorization")) {
		return &AuthPrincipal{Kind: "gateway_bearer", Username: cfg.XGAuthUsername}
	}
	if cfg.XGAuthCookie != "" {
		if cookie, err := r.Cookie(cfg.XGAuthCookie); err == nil && gatewayTokenAuthenticated(cfg, cookie.Value) {
			return &AuthPrincipal{Kind: "gateway_cookie", Username: cfg.XGAuthUsername}
		}
	}
	return nil
}

func voterKeyFromRequest(principal *AuthPrincipal, r *http.Request) string {
	if principal != nil {
		if principal.APIKeyID > 0 {
			return fmt.Sprintf("api_key:%d", principal.APIKeyID)
		}
		if principal.UserID > 0 {
			return fmt.Sprintf("user:%d", principal.UserID)
		}
	}
	if r == nil {
		return ""
	}
	if apiKey := requestAPIKey(r.Header); apiKey != "" {
		return "static_api_key:" + shortHash(apiKey)
	}
	if authorization := strings.TrimSpace(r.Header.Get("Authorization")); authorization != "" {
		return "authorization:" + shortHash(authorization)
	}
	if globalConfig.XGAuthCookie != "" {
		if cookie, err := r.Cookie(globalConfig.XGAuthCookie); err == nil && strings.TrimSpace(cookie.Value) != "" {
			return "cookie:" + shortHash(cookie.Value)
		}
	}
	return ""
}

func shortHash(value string) string {
	sum := sha256.Sum256([]byte(strings.TrimSpace(value)))
	return hex.EncodeToString(sum[:])[:24]
}

func authenticateUserRequest(cfg Config, r *http.Request) *AuthPrincipal {
	if r == nil {
		return nil
	}
	if principal := authenticateUserBearer(cfg, r.Header.Get("Authorization")); principal != nil {
		return principal
	}
	if principal := authenticateServiceAPIKey(r.Context(), cfg, r.Header); principal != nil && principal.UserID > 0 {
		return principal
	}
	return nil
}

func gatewayBearerAuthenticated(cfg Config, authorization string) bool {
	authorization = strings.TrimSpace(authorization)
	if !strings.HasPrefix(strings.ToLower(authorization), "bearer ") {
		return false
	}
	return gatewayTokenAuthenticated(cfg, strings.TrimSpace(authorization[len("bearer "):]))
}

func gatewayTokenAuthenticated(cfg Config, token string) bool {
	token = strings.TrimSpace(token)
	if token == "" || !strings.Contains(token, ".") || strings.TrimSpace(cfg.XGAuthSecret) == "" {
		return false
	}
	payloadB64, signatureHex, ok := strings.Cut(token, ".")
	if !ok || payloadB64 == "" || signatureHex == "" {
		return false
	}

	signature := hmac.New(sha256.New, []byte(cfg.XGAuthSecret))
	signature.Write([]byte(payloadB64))
	expectedSignature := fmt.Sprintf("%x", signature.Sum(nil))
	if !hmac.Equal([]byte(signatureHex), []byte(expectedSignature)) {
		return false
	}

	payloadRaw, err := base64.RawURLEncoding.DecodeString(payloadB64)
	if err != nil {
		return false
	}
	var payload map[string]string
	if err := json.Unmarshal(payloadRaw, &payload); err != nil {
		return false
	}
	return strings.TrimSpace(payload["username"]) == strings.TrimSpace(cfg.XGAuthUsername)
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
