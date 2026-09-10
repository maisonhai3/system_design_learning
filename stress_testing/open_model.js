import http from 'k6/http';
import { check } from 'k6';

export const options = {
  scenarios: {
    open_model_stress: {
      executor: 'ramping-arrival-rate',
      // Start at 10 iterations per second
      startRate: 10,
      timeUnit: '1s',
      // You must pre-allocate enough VUs to handle the pending requests
      // if the server slows down
      preAllocatedVUs: 500,
      maxVUs: 5000,
      stages: [
        { duration: '1s', target: 20 },  // Ramp to 50 req/s
        // { duration: '5m', target: 50 },  // Hold normal load
        // { duration: '2m', target: 200 }, // Spike to 200 req/s (STRESS)
        // { duration: '5m', target: 200 }, // Hold STRESS load
        // { duration: '2m', target: 0 },   // Scale down
      ],
    },
  },
  thresholds: {
    http_req_duration: ['p(99)<2000'],
    http_req_failed: ['rate<0.05'],
  },
};

export default function () {
  const res = http.get('https://vnexpress.net');

  check(res, {
    'status is 200': (r) => r.status === 200,
  });

  // Note: sleep() is generally omitted in arrival-rate models
  // because the pacing is controlled entirely by the executor.
}
