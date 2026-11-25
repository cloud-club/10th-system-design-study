package main

// 웹 서버에서 도는 제어기 (ROLE=ctl, 127.0.0.1:9000, nginx 가 /admin · /ctl 을 basic auth 로 보호).
// 이벤트 설정 → WAS 리셋 → k6 가상 트래픽 실행 → 1초마다 지표 기록 → 큐 소진 후 정합성 검증.

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"sync"
	"time"
)

//go:embed admin.html
var adminHTML []byte

type runConfig struct {
	Name    string `json:"name"`
	Tickets int    `json:"tickets"`
	Users   int    `json:"users"`
	RampSec int    `json:"rampSec"`
	DupPct  int    `json:"dupPct"`
	Poll    bool   `json:"poll"`
	Rate    int    `json:"workerRate"` // 워커 초당 처리 한도, 0 = 무제한
}

type sample struct {
	T        float64 `json:"t"` // 시작 후 초
	Sent     int64   `json:"sent"`
	RPS      float64 `json:"rps"`
	Accepted int64   `json:"accepted"`
	SoldOut  int64   `json:"soldOut"`
	Failed   int64   `json:"failed"`
	P95      float64 `json:"p95"`
	VUs      int64   `json:"vus"`
	Stock    int64   `json:"stock"`
	Orders   int64   `json:"orders"`
	Done     int64   `json:"done"`
	DoneRate float64 `json:"doneRate"`
	Lag      int64   `json:"lag"`
}

type run struct {
	ID       int            `json:"id"`
	Config   runConfig      `json:"config"`
	Started  time.Time      `json:"started"`
	Phase    string         `json:"phase"` // load → drain → done | stopped | error
	Timeline []sample       `json:"timeline"`
	Summary  map[string]any `json:"summary,omitempty"` // k6 종료 요약
	Final    map[string]any `json:"final,omitempty"`   // WAS /api/stats 최종값
	Checks   []check        `json:"checks,omitempty"`
	Pass     bool           `json:"pass"`
	Error    string         `json:"error,omitempty"`
}

type check struct {
	Name string `json:"name"`
	OK   bool   `json:"ok"`
	Info string `json:"info"`
}

var (
	ctlMu   sync.Mutex
	current *run
	history []*run
	k6Cmd   *exec.Cmd
	wasURL  = env("WAS_URL", "http://127.0.0.1:8080")
	loadURL = env("LOAD_TARGET", "http://127.0.0.1")
	k6Dir   = env("K6_DIR", "/opt/fcfs")
)

func runCtl() {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /admin", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write(adminHTML)
	})
	mux.HandleFunc("GET /ctl/state", ctlState)
	mux.HandleFunc("POST /ctl/run", ctlRun)
	mux.HandleFunc("POST /ctl/stop", ctlStop)
	log.Printf("ctl listening 127.0.0.1:9000, WAS=%s, load target=%s", wasURL, loadURL)
	log.Fatal(http.ListenAndServe("127.0.0.1:9000", mux))
}

func ctlState(w http.ResponseWriter, r *http.Request) {
	ctlMu.Lock()
	defer ctlMu.Unlock()
	hist := make([]map[string]any, 0, len(history))
	for i := len(history) - 1; i >= 0; i-- {
		h := history[i]
		hist = append(hist, map[string]any{"id": h.ID, "config": h.Config, "started": h.Started, "phase": h.Phase, "pass": h.Pass, "summary": h.Summary, "final": h.Final})
	}
	b, _ := json.Marshal(map[string]any{"current": current, "history": hist})
	w.Header().Set("Content-Type", "application/json")
	w.Write(b)
}

func ctlRun(w http.ResponseWriter, r *http.Request) {
	var c runConfig
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&c); err != nil {
		http.Error(w, "bad json", 400)
		return
	}
	switch {
	case c.Tickets < 1 || c.Tickets > 1_000_000:
		http.Error(w, "티켓 수량은 1 ~ 1,000,000", 400)
		return
	case c.Users < 1 || c.Users > 200_000:
		http.Error(w, "참여자 수는 1 ~ 200,000", 400)
		return
	case c.RampSec < 1 || c.RampSec > 600:
		http.Error(w, "유입 시간은 1 ~ 600초", 400)
		return
	case c.Rate < 0 || c.Rate > 100_000:
		http.Error(w, "워커 처리 한도는 0(무제한) ~ 100,000건/초", 400)
		return
	case c.DupPct < 0 || c.DupPct > 100:
		http.Error(w, "중복 클릭 비율은 0 ~ 100%", 400)
		return
	case (c.Users+c.RampSec-1)/c.RampSec > 15_000:
		http.Error(w, "초당 유입이 15,000을 넘습니다. 유입 시간을 늘리세요 (부하 발생기 1대 한계)", 400)
		return
	}
	if c.Name == "" {
		c.Name = "선착순 이벤트"
	}
	ctlMu.Lock()
	if current != nil && (current.Phase == "load" || current.Phase == "drain") {
		ctlMu.Unlock()
		http.Error(w, "이미 실행 중", 409)
		return
	}
	ctlMu.Unlock()

	req, _ := http.NewRequest("POST", fmt.Sprintf("%s/admin/reset?qty=%d&name=%s&workerRate=%d", wasURL, c.Tickets, url.QueryEscape(c.Name), c.Rate), nil)
	req.Header.Set("X-Admin-Token", os.Getenv("ADMIN_TOKEN"))
	resp, err := http.DefaultClient.Do(req)
	if err != nil || resp.StatusCode != 200 {
		http.Error(w, fmt.Sprintf("WAS 리셋 실패: %v", err), 502)
		return
	}
	resp.Body.Close()
	os.Remove(k6Dir + "/summary.json")
	cmd := exec.Command("k6", "run", "-q", "--address", "127.0.0.1:6565", k6Dir+"/ticket.js")
	cmd.Env = append(os.Environ(),
		"BASE="+loadURL, fmt.Sprintf("USERS=%d", c.Users), fmt.Sprintf("RAMP=%d", c.RampSec),
		fmt.Sprintf("DUP=%d", c.DupPct), fmt.Sprintf("POLL=%t", c.Poll), "SUMMARY="+k6Dir+"/summary.json")
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if err := cmd.Start(); err != nil {
		http.Error(w, "k6 실행 실패: "+err.Error(), 500)
		return
	}
	ctlMu.Lock()
	ru := &run{ID: len(history) + 1, Config: c, Started: time.Now(), Phase: "load"}
	current, k6Cmd = ru, cmd
	history = append(history, ru)
	if len(history) > 20 {
		history = history[len(history)-20:]
	}
	ctlMu.Unlock()
	go supervise(ru, cmd)
	w.WriteHeader(202)
	json.NewEncoder(w).Encode(map[string]int{"id": ru.ID})
}

func ctlStop(w http.ResponseWriter, r *http.Request) {
	ctlMu.Lock()
	defer ctlMu.Unlock()
	if k6Cmd != nil && k6Cmd.Process != nil && current != nil && current.Phase == "load" {
		k6Cmd.Process.Kill()
		current.Phase = "stopped"
	}
	w.WriteHeader(204)
}

func supervise(ru *run, cmd *exec.Cmd) {
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()
	tick := time.NewTicker(500 * time.Millisecond)
	defer tick.Stop()
	var prev sample
	k6Done := false
	for {
		select {
		case <-exited:
			k6Done = true
		case <-tick.C:
		}
		s := prev
		s.T = time.Since(ru.Started).Seconds()
		if !k6Done {
			readK6(&s)
		}
		st := getJSON(wasURL + "/api/stats")
		if st != nil {
			s.Stock, s.Orders, s.Lag = num(st["stock"]), num(st["orders"]), num(st["lag"])
			var done int64
			if res, ok := st["results"].(map[string]any); ok {
				for _, v := range res {
					done += num(v)
				}
			}
			s.Done = done
		}
		// 초당 값은 최근 1초 구간 평균 (0.5초 간격이라 직전 값과만 비교하면 배치 단위로 출렁임)
		if n := len(ru.Timeline); n > 0 {
			base := ru.Timeline[0]
			for i := n - 1; i >= 0; i-- {
				if s.T-ru.Timeline[i].T >= 1.0 {
					base = ru.Timeline[i]
					break
				}
			}
			if dt := s.T - base.T; dt > 0 {
				s.RPS = float64(s.Sent-base.Sent) / dt
				s.DoneRate = float64(s.Done-base.Done) / dt
			}
		}
		if k6Done {
			s.RPS = 0
		}
		ctlMu.Lock()
		ru.Timeline = append(ru.Timeline, s)
		if k6Done && ru.Phase == "load" {
			ru.Phase = "drain"
		}
		phase := ru.Phase
		ctlMu.Unlock()
		prev = s
		if k6Done && (st != nil && s.Lag == 0 || s.T > 900) {
			finish(ru, st, phase)
			return
		}
		if k6Done && phase == "stopped" {
			finish(ru, st, phase)
			return
		}
	}
}

// k6 REST API(--address) 에서 실시간 지표를 읽는다.
func readK6(s *sample) {
	m := getJSON("http://127.0.0.1:6565/v1/metrics")
	if m == nil {
		return
	}
	data, _ := m["data"].([]any)
	for _, d := range data {
		dm, _ := d.(map[string]any)
		attrs, _ := dm["attributes"].(map[string]any)
		smp, _ := attrs["sample"].(map[string]any)
		switch dm["id"] {
		case "http_reqs":
			s.Sent = num(smp["count"])
		case "fcfs_accepted":
			s.Accepted = num(smp["count"])
		case "fcfs_soldout":
			s.SoldOut = num(smp["count"])
		case "fcfs_failed":
			s.Failed = num(smp["count"])
		case "fcfs_submit_ms":
			if v, ok := smp["p(95)"].(float64); ok {
				s.P95 = v
			}
		case "vus":
			s.VUs = num(smp["value"])
		}
	}
}

func finish(ru *run, st map[string]any, phase string) {
	var summary map[string]any
	if b, err := os.ReadFile(k6Dir + "/summary.json"); err == nil {
		json.Unmarshal(b, &summary)
	}
	ctlMu.Lock()
	defer ctlMu.Unlock()
	ru.Summary, ru.Final = summary, st
	if phase == "stopped" || st == nil {
		if st == nil {
			ru.Phase, ru.Error = "error", "WAS 지표 조회 실패"
		}
		return
	}
	stock, initial, orders, users, lag := num(st["stock"]), num(st["initial"]), num(st["orders"]), num(st["distinctUsers"]), num(st["lag"])
	var done int64
	if res, ok := st["results"].(map[string]any); ok {
		for _, v := range res {
			done += num(v)
		}
	}
	accepted := num(summary["accepted"])
	ru.Checks = []check{
		{"초과 판매 없음", orders <= initial && stock >= 0, fmt.Sprintf("주문 %d / 티켓 %d, 남은 재고 %d", orders, initial, stock)},
		{"중복 당첨 없음", orders == users, fmt.Sprintf("주문 %d건 = 고유 사용자 %d명", orders, users)},
		{"수량 보존 (재고 + 주문 = 총량)", stock+orders == initial, fmt.Sprintf("%d + %d = %d", stock, orders, initial)},
		{"접수 유실 없음", done >= accepted, fmt.Sprintf("처리 결과 %d건 ≥ 접수(202) %d건", done, accepted)},
		{"큐 소진 (lag 0)", lag == 0, fmt.Sprintf("lag %d", lag)},
	}
	ru.Pass = true
	for _, c := range ru.Checks {
		ru.Pass = ru.Pass && c.OK
	}
	ru.Phase = "done"
}

func getJSON(u string) map[string]any {
	c := http.Client{Timeout: 2 * time.Second}
	resp, err := c.Get(u)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	var m map[string]any
	if json.Unmarshal(b, &m) != nil {
		return nil
	}
	return m
}

func num(v any) int64 {
	f, _ := v.(float64)
	return int64(f)
}
