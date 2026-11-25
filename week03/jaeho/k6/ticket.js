// 실시간 티케팅 가상 트래픽. 웹 서버의 제어기(ctl)가 env 로 설정을 넘겨 실행한다.
//   USERS  참여자 수 (각자 고유 ID로 1번 참여)
//   RAMP   유입 시간(초) — 이 시간 동안 균등하게 몰려온다
//   DUP    중복 클릭 비율(%) — 해당 비율의 사용자는 "참여하기"를 두 번 누른다
//   POLL   true 면 접수(202)된 사용자가 결과가 나올 때까지 1초마다 조회
import http from 'k6/http';
import exec from 'k6/execution';
import { sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const BASE = __ENV.BASE, USERS = +__ENV.USERS, RAMP = +__ENV.RAMP;
const DUP = +__ENV.DUP / 100, POLL = __ENV.POLL === 'true';
const RATE = Math.ceil(USERS / RAMP);

const accepted = new Counter('fcfs_accepted');
const soldout = new Counter('fcfs_soldout');
const failed = new Counter('fcfs_failed');
const won = new Counter('fcfs_won');
const submitMs = new Trend('fcfs_submit_ms', true);   // 참여 버튼 → 응답
const resultMs = new Trend('fcfs_result_ms', true);   // 참여 버튼 → 최종 결과 확인

export const options = {
  discardResponseBodies: true,
  summaryTrendStats: ['avg', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    ticketing: {
      executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: `${RAMP}s`,
      preAllocatedVUs: Math.min(RATE * 2, 4000),
      maxVUs: Math.min(POLL ? RATE * 15 : RATE * 2, 12000) + 100,
      gracefulStop: '30s',
    },
  },
};

const H = { headers: { 'Content-Type': 'application/json' }, timeout: '10s', responseType: 'text' };

function submit(uid) {
  const t0 = Date.now();
  const r = http.post(`${BASE}/api/orders`, JSON.stringify({ userId: uid }), H);
  submitMs.add(Date.now() - t0);
  if (r.status === 202) { accepted.add(1); return { t0, id: r.json('requestId') }; }
  if (r.status === 200) { soldout.add(1); resultMs.add(Date.now() - t0); return null; }
  failed.add(1); return null;
}

export default function () {
  const uid = exec.scenario.iterationInTest + 1;
  if (uid > USERS) return;
  const reqs = [submit(uid)];
  if (Math.random() < DUP) reqs.push(submit(uid));          // 중복 클릭
  if (!POLL) return;
  for (const q of reqs.filter(Boolean)) {
    for (let i = 0; i < 30; i++) {
      sleep(1);
      const r = http.get(`${BASE}/api/orders/${q.id}`, { timeout: '5s', responseType: 'text', tags: { name: 'status' } });
      if (r.status !== 200) continue;
      const st = r.json('status');
      if (st !== 'PENDING') { resultMs.add(Date.now() - q.t0); if (st === 'SUCCESS') won.add(1); break; }
    }
  }
}

export function handleSummary(d) {
  const c = (k) => (d.metrics[k] ? d.metrics[k].values.count || 0 : 0);
  const t = (k, p) => (d.metrics[k] ? Math.round(d.metrics[k].values[p] || 0) : null);
  const out = {
    users: USERS, rampSec: RAMP, targetRps: RATE,
    requests: c('http_reqs'), rps: Math.round(d.metrics.http_reqs ? d.metrics.http_reqs.values.rate : 0),
    accepted: c('fcfs_accepted'), soldout: c('fcfs_soldout'), failed: c('fcfs_failed'), won: c('fcfs_won'),
    submitP95: t('fcfs_submit_ms', 'p(95)'), submitP99: t('fcfs_submit_ms', 'p(99)'),
    resultP95: t('fcfs_result_ms', 'p(95)'), resultMax: t('fcfs_result_ms', 'max'),
    droppedIterations: c('dropped_iterations'),
  };
  return { [__ENV.SUMMARY]: JSON.stringify(out), stdout: JSON.stringify(out) + '\n' };
}
