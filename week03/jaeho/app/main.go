// fcfs: 선착순 POC. 한 바이너리를 ROLE 로 나눠 띄운다.
//
//	ROLE=api    WAS: 접수(→Kafka) · 결과 조회 · 관제 지표
//	ROLE=worker 워커: Kafka → MySQL (HTTP 없음)
//	ROLE=ctl    웹 서버의 제어기: 이벤트 설정 · k6 가상 트래픽 실행 · 결과 검증 (ctl.go)
package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	_ "github.com/go-sql-driver/mysql"
	"github.com/twmb/franz-go/pkg/kadm"
	"github.com/twmb/franz-go/pkg/kerr"
	"github.com/twmb/franz-go/pkg/kgo"
)

//go:embed index.html
var indexHTML []byte

const (
	topic   = "order-req"
	group   = "fcfs-workers"
	eventID = 1
)

var (
	db       *sql.DB
	cl       *kgo.Client
	adm      *kadm.Client
	host, _  = os.Hostname()
	soldOut  atomic.Bool
	baseline atomic.Pointer[map[int32]int64] // 마지막 리셋 시점의 파티션별 끝 오프셋
	accepted atomic.Int64
)

type orderMsg struct {
	RequestID string `json:"requestId"`
	UserID    int64  `json:"userId"`
	TS        int64  `json:"ts"`
}

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func main() {
	role := env("ROLE", "api")
	if role == "ctl" {
		runCtl()
		return
	}
	var err error
	db, err = sql.Open("mysql", os.Getenv("MYSQL_DSN"))
	must(err)
	db.SetMaxOpenConns(64)
	db.SetMaxIdleConns(64)
	must(migrate())

	brokers := strings.Split(os.Getenv("KAFKA_BROKERS"), ",")
	opts := []kgo.Opt{kgo.SeedBrokers(brokers...), kgo.RequiredAcks(kgo.AllISRAcks()), kgo.ProducerLinger(2 * time.Millisecond)}
	cl, err = kgo.NewClient(opts...)
	must(err)
	adm = kadm.NewClient(cl)
	ensureTopic()

	if role == "worker" {
		runWorker(brokers)
		return
	}
	go refreshSoldOut()

	mux := http.NewServeMux()
	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write(indexHTML)
	})
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
	mux.HandleFunc("POST /api/orders", handleOrder)
	mux.HandleFunc("GET /api/orders/{id}", handleStatus)
	mux.HandleFunc("GET /api/stats", handleStats)
	mux.HandleFunc("POST /admin/reset", handleReset)
	log.Printf("fcfs %s listening :8080", host)
	srv := &http.Server{Addr: ":8080", Handler: mux, ReadTimeout: 5 * time.Second, WriteTimeout: 10 * time.Second}
	log.Fatal(srv.ListenAndServe())
}

func must(err error) {
	if err != nil {
		log.Fatal(err)
	}
}

func migrate() error {
	for _, q := range []string{
		`CREATE TABLE IF NOT EXISTS stock (event_id BIGINT PRIMARY KEY, qty INT NOT NULL, initial_qty INT NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS orders (
			id BIGINT AUTO_INCREMENT PRIMARY KEY, event_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
			request_id CHAR(32) NOT NULL, created_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
			UNIQUE KEY uk_event_user (event_id, user_id), UNIQUE KEY uk_request (request_id))`,
		`CREATE TABLE IF NOT EXISTS order_result (
			request_id CHAR(32) PRIMARY KEY, user_id BIGINT NOT NULL, status VARCHAR(16) NOT NULL,
			worker VARCHAR(64) NOT NULL, updated_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3))`,
		`INSERT IGNORE INTO stock(event_id,qty,initial_qty) VALUES (1, 10000, 10000)`,
		`CREATE TABLE IF NOT EXISTS settings (k VARCHAR(32) PRIMARY KEY, v BIGINT NOT NULL)`,
	} {
		if _, err := db.Exec(q); err != nil {
			return err
		}
	}
	db.Exec(`ALTER TABLE stock ADD COLUMN name VARCHAR(100) NOT NULL DEFAULT '선착순 이벤트'`) // 이미 있으면 에러 → 무시
	return nil
}

func ensureTopic() {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	min := "2"
	resp, err := adm.CreateTopic(ctx, 12, 3, map[string]*string{"min.insync.replicas": &min}, topic)
	if err == nil {
		err = resp.Err
	}
	if err != nil && !errors.Is(err, kerr.TopicAlreadyExists) {
		log.Printf("create topic: %v", err)
	}
}

// 재고가 0이면 API가 Kafka까지 가지 않고 즉시 매진 응답 → 큐에는 재고 근처 양만 쌓인다.
// ponytail: 300ms 폴링 캐시라 매진 직후 잠깐 더 받아들인다. 최종 판정은 워커/DB가 하므로 정합성엔 영향 없음.
func refreshSoldOut() {
	for {
		var q int
		if err := db.QueryRow(`SELECT qty FROM stock WHERE event_id=?`, eventID).Scan(&q); err == nil {
			soldOut.Store(q <= 0)
		}
		time.Sleep(300 * time.Millisecond)
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Instance", host)
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func newID() string {
	b := make([]byte, 16)
	rand.Read(b)
	return hex.EncodeToString(b)
}

func handleOrder(w http.ResponseWriter, r *http.Request) {
	var in struct {
		UserID int64 `json:"userId"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024)).Decode(&in); err != nil || in.UserID <= 0 {
		writeJSON(w, 400, map[string]string{"error": "userId required"})
		return
	}
	if soldOut.Load() {
		writeJSON(w, 200, map[string]string{"status": "SOLD_OUT", "instance": host})
		return
	}
	m := orderMsg{RequestID: newID(), UserID: in.UserID, TS: time.Now().UnixMilli()}
	val, _ := json.Marshal(m)
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	rec := &kgo.Record{Topic: topic, Key: []byte(strconv.FormatInt(m.UserID, 10)), Value: val}
	if err := cl.ProduceSync(ctx, rec).FirstErr(); err != nil {
		writeJSON(w, 503, map[string]string{"error": "queue unavailable, retry", "instance": host})
		return
	}
	accepted.Add(1)
	writeJSON(w, 202, map[string]string{"requestId": m.RequestID, "status": "PENDING", "instance": host})
}

func handleStatus(w http.ResponseWriter, r *http.Request) {
	var st string
	err := db.QueryRowContext(r.Context(), `SELECT status FROM order_result WHERE request_id=?`, r.PathValue("id")).Scan(&st)
	if errors.Is(err, sql.ErrNoRows) {
		st = "PENDING"
	} else if err != nil {
		writeJSON(w, 503, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, 200, map[string]string{"status": st})
}

// 관제 지표: 0.4초 캐시 → 화면을 몇 명이 0.5초마다 보든 DB·Kafka 조회는 초당 최대 2~3번
var (
	statsMu   sync.Mutex
	statsBody []byte
	statsAt   time.Time
)

func handleStats(w http.ResponseWriter, r *http.Request) {
	statsMu.Lock()
	if time.Since(statsAt) > 400*time.Millisecond {
		statsBody, _ = json.Marshal(buildStats(r.Context()))
		statsAt = time.Now()
	}
	b := statsBody
	statsMu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.Write(b)
}

func buildStats(rctx context.Context) map[string]any {
	ctx, cancel := context.WithTimeout(rctx, 3*time.Second)
	defer cancel()
	out := map[string]any{"instance": host, "acceptedHere": accepted.Load(), "ts": time.Now().UnixMilli()}
	var qty, initial, orders, users int64
	var name string
	db.QueryRowContext(ctx, `SELECT qty, initial_qty, name FROM stock WHERE event_id=?`, eventID).Scan(&qty, &initial, &name)
	out["name"] = name
	db.QueryRowContext(ctx, `SELECT COUNT(*), COUNT(DISTINCT user_id) FROM orders WHERE event_id=?`, eventID).Scan(&orders, &users)
	res := map[string]int64{}
	if rows, err := db.QueryContext(ctx, `SELECT status, COUNT(*) FROM order_result GROUP BY status`); err == nil {
		for rows.Next() {
			var s string
			var n int64
			rows.Scan(&s, &n)
			res[s] = n
		}
		rows.Close()
	}
	var lag int64 = -1
	var glag kadm.GroupLag
	if lags, err := adm.Lag(ctx, group); err == nil {
		if gl, ok := lags[group]; ok && gl.Error() == nil {
			lag, glag = gl.Lag.Total(), gl.Lag
		}
	}
	out["kafka"] = kafkaView(ctx, glag)
	byWorker := map[string]int64{}
	if rows, err := db.QueryContext(ctx, `SELECT worker, COUNT(*) FROM order_result GROUP BY worker`); err == nil {
		for rows.Next() {
			var w string
			var n int64
			rows.Scan(&w, &n)
			byWorker[w] = n
		}
		rows.Close()
	}
	out["byWorker"] = byWorker
	out["stock"], out["initial"], out["orders"], out["distinctUsers"], out["results"], out["lag"] = qty, initial, orders, users, res, lag
	out["oversold"] = orders > initial || qty < 0
	return out
}

func handleReset(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("X-Admin-Token") != os.Getenv("ADMIN_TOKEN") || os.Getenv("ADMIN_TOKEN") == "" {
		writeJSON(w, 403, map[string]string{"error": "forbidden"})
		return
	}
	qty, _ := strconv.Atoi(r.URL.Query().Get("qty"))
	if qty <= 0 {
		qty = 10000
	}
	for _, q := range []string{`TRUNCATE orders`, `TRUNCATE order_result`} {
		if _, err := db.Exec(q); err != nil {
			writeJSON(w, 500, map[string]string{"error": err.Error()})
			return
		}
	}
	name := r.URL.Query().Get("name")
	if name == "" || len([]rune(name)) > 100 {
		name = "선착순 이벤트"
	}
	db.Exec(`REPLACE INTO stock(event_id,qty,initial_qty,name) VALUES (?, ?, ?, ?)`, eventID, qty, qty, name)
	wr, _ := strconv.Atoi(r.URL.Query().Get("workerRate"))
	db.Exec(`REPLACE INTO settings VALUES ('worker_rate', ?)`, max(wr, 0))
	if ends, err := adm.ListEndOffsets(r.Context(), topic); err == nil { // 이번 회차 유입량 계산 기준점
		b := map[int32]int64{}
		ends.Each(func(o kadm.ListedOffset) { b[o.Partition] = o.Offset })
		baseline.Store(&b)
	}
	writeJSON(w, 200, map[string]any{"reset": qty})
}

// ---------------- worker ----------------

func runWorker(brokers []string) {
	c, err := kgo.NewClient(
		kgo.SeedBrokers(brokers...),
		kgo.ConsumerGroup(group), kgo.ConsumeTopics(topic), kgo.ClientID(host),
		kgo.Balancers(kgo.CooperativeStickyBalancer()),
		kgo.DisableAutoCommit(), kgo.BlockRebalanceOnPoll(),
		kgo.SessionTimeout(10*time.Second), kgo.HeartbeatInterval(2*time.Second),
		kgo.FetchMaxWait(200*time.Millisecond),
	)
	must(err)
	log.Printf("worker %s started", host)
	var rate atomic.Int64 // 초당 처리 한도 (0 = 무제한). 관리 콘솔에서 설정 → settings 테이블
	go func() {
		for {
			var v int64
			if db.QueryRow(`SELECT v FROM settings WHERE k='worker_rate'`).Scan(&v) == nil {
				rate.Store(v)
			}
			time.Sleep(time.Second)
		}
	}()
	ctx := context.Background()
	for {
		start := time.Now()
		n := 500
		if r := rate.Load(); r > 0 { // 한도가 있으면 0.2초 분량씩 잘게 → 처리 흐름이 계단 없이 고르게
			n = int(min(500, max(1, r/5)))
		}
		fs := c.PollRecords(ctx, n)
		fs.EachError(func(t string, p int32, err error) { log.Printf("fetch %s/%d: %v", t, p, err) })
		recs := fs.Records()
		for len(recs) > 0 {
			if err := processBatch(recs); err != nil {
				log.Printf("batch failed, retry: %v", err) // 커밋하지 않고 같은 배치를 재시도 → 유실 없음
				time.Sleep(time.Second)
				continue
			}
			break
		}
		if len(recs) > 0 {
			if err := c.CommitRecords(ctx, recs...); err != nil {
				log.Printf("commit: %v", err) // 커밋 실패 시 재처리되지만 uk_request 로 멱등
			}
		}
		c.AllowRebalance()
		if r := rate.Load(); r > 0 && len(recs) > 0 { // "DB가 감당하는 속도" 흉내: 배치 크기 / 한도 만큼은 걸리게
			time.Sleep(time.Until(start.Add(time.Duration(float64(len(recs)) / float64(r) * float64(time.Second)))))
		}
	}
}

// 배치 하나 = 트랜잭션 하나, DB 왕복 약 6회 (레코드 수와 무관).
// 재고 행을 FOR UPDATE 로 잠그고, 남은 재고만큼만 INSERT IGNORE 로 넣어 본 뒤 실제로 들어간 행 수만큼 차감한다.
//   - UNIQUE(event,user) 에 막힌 건 → DUPLICATE
//   - UNIQUE(request) 에 막힌 재전달 → orders 에 이미 있으므로 SUCCESS 로 다시 기록 (멱등)
//
// 차감량은 잠근 재고 이하이므로 qty 가 음수가 될 수 없다.
func processBatch(recs []*kgo.Record) error {
	msgs := make([]orderMsg, 0, len(recs))
	for _, rec := range recs {
		var m orderMsg
		if json.Unmarshal(rec.Value, &m) == nil && m.RequestID != "" {
			msgs = append(msgs, m) // 형식 불량 메시지는 버린다
		}
	}
	if len(msgs) == 0 {
		return nil
	}
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var qty int
	if err := tx.QueryRow(`SELECT qty FROM stock WHERE event_id=? FOR UPDATE`, eventID).Scan(&qty); err != nil {
		return err
	}
	status := make(map[string]string, len(msgs))
	used, i := 0, 0
	for i < len(msgs) && qty > 0 {
		chunk := msgs[i : i+min(qty, len(msgs)-i)]
		i += len(chunk)
		args := make([]any, 0, len(chunk)*3)
		for _, m := range chunk {
			args = append(args, eventID, m.UserID, m.RequestID)
		}
		res, err := tx.Exec(`INSERT IGNORE INTO orders(event_id,user_id,request_id) VALUES `+rowsPH(len(chunk), 3), args...)
		if err != nil {
			return err
		}
		n, _ := res.RowsAffected()
		qty -= int(n)
		used += int(n)
		if err := markExisting(tx, chunk, status); err != nil {
			return err
		}
		for _, m := range chunk {
			if status[m.RequestID] == "" {
				status[m.RequestID] = "DUPLICATE"
			}
		}
	}
	if used > 0 {
		if _, err := tx.Exec(`UPDATE stock SET qty=qty-? WHERE event_id=?`, used, eventID); err != nil {
			return err
		}
	}
	if rest := msgs[i:]; len(rest) > 0 { // 재고 소진 후 남은 것: 재전달된 당첨자는 SUCCESS 유지, 나머지는 SOLD_OUT
		if err := markExisting(tx, rest, status); err != nil {
			return err
		}
		for _, m := range rest {
			if status[m.RequestID] == "" {
				status[m.RequestID] = "SOLD_OUT"
			}
		}
	}
	args := make([]any, 0, len(msgs)*4)
	for _, m := range msgs {
		args = append(args, m.RequestID, m.UserID, status[m.RequestID], host)
	}
	if _, err := tx.Exec(`INSERT INTO order_result(request_id,user_id,status,worker) VALUES `+rowsPH(len(msgs), 4)+
		` ON DUPLICATE KEY UPDATE status=VALUES(status), worker=VALUES(worker)`, args...); err != nil {
		return fmt.Errorf("results: %w", err)
	}
	return tx.Commit()
}

// orders 에 이미 존재하는 request_id 를 SUCCESS 로 표시
func markExisting(tx *sql.Tx, ms []orderMsg, status map[string]string) error {
	args := make([]any, len(ms))
	for i, m := range ms {
		args[i] = m.RequestID
	}
	rows, err := tx.Query(`SELECT request_id FROM orders WHERE request_id IN (`+strings.TrimSuffix(strings.Repeat("?,", len(ms)), ",")+`)`, args...)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return err
		}
		status[id] = "SUCCESS"
	}
	return rows.Err()
}

// rowsPH(2,3) → "(?,?,?),(?,?,?)"
func rowsPH(n, cols int) string {
	row := "(" + strings.TrimSuffix(strings.Repeat("?,", cols), ",") + ")"
	return strings.TrimSuffix(strings.Repeat(row+",", n), ",")
}

// 관제용 Kafka 상태: 브로커별 리더 수, 파티션별 적재·대기·리더·ISR·담당 워커
func kafkaView(ctx context.Context, gl kadm.GroupLag) map[string]any {
	md, err := adm.Metadata(ctx, topic)
	if err != nil {
		return map[string]any{"error": err.Error()}
	}
	base := map[int32]int64{}
	if b := baseline.Load(); b != nil {
		base = *b
	}
	leaders := map[int32]int{}
	td := md.Topics[topic]
	parts := make([]map[string]any, len(td.Partitions))
	for _, p := range td.Partitions.Sorted() {
		leaders[p.Leader]++
		row := map[string]any{"p": p.Partition, "leader": p.Leader, "replicas": len(p.Replicas), "isr": len(p.ISR), "lag": int64(-1), "end": int64(-1), "in": int64(0), "worker": ""}
		if ml, ok := gl[topic][p.Partition]; ok {
			row["lag"], row["end"] = ml.Lag, ml.End.Offset
			if b, ok := base[p.Partition]; ok {
				row["in"] = ml.End.Offset - b
			}
			if ml.Member != nil {
				row["worker"] = ml.Member.ClientID
			}
		}
		if int(p.Partition) < len(parts) {
			parts[p.Partition] = row
		}
	}
	brokers := []map[string]any{}
	for _, b := range md.Brokers {
		brokers = append(brokers, map[string]any{"id": b.NodeID, "host": b.Host, "leaders": leaders[b.NodeID]})
	}
	return map[string]any{"topic": topic, "partitions": parts, "brokers": brokers, "group": group}
}
