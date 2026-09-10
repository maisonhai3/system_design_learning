import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '2m', target: 1000 }, // Ramp-up to normal load
    // { duration: '5m', target: 100 }, // Hold normal load
    // { duration: '2m', target: 500 }, // Ramp-up to STRESS load
    // { duration: '5m', target: 500 }, // Hold STRESS load
    // { duration: '2m', target: 0 },   // Scale down (Recovery phase)
  ],
  thresholds: {
    http_req_duration: ['p(99)<2000'], // 99% of requests must complete below 2s
    http_req_failed: ['rate<0.05'],    // Error rate must be less than 5%
  },
};

export default function () {
  const res = http.get('https://test-api.k6.io/public/crocodiles/1/');

  check(res, {
    'status is 200': (r) => r.status === 200,
    'transaction OK': (r) => r.timings.duration < 1000, // custom check
  });

  // Pacing is critical: VUs need to sleep to simulate real users
  // rather than blindly hammering the server as fast as possible.
  sleep(1);
}
