// Load test of the library API at peak hours, focused on the full-library search.
//
// Fake readers open the app, search the whole library (most of the traffic), search the
// contents lists, read pages online and download books, pausing between actions like
// real people. Run with k6 (https://k6.io):
//
//   k6 run scripts/loadtest/library.js                         # baseline: 5 users, 5 minutes
//   k6 run -e STAGE=ramp scripts/loadtest/library.js           # 10 -> 25 -> 50 -> 100 users
//   k6 run -e STAGE=spike scripts/loadtest/library.js          # straight to PEAK users for 2 minutes
//   k6 run -e STAGE=soak -e PEAK=50 scripts/loadtest/library.js   # PEAK users for 30 minutes
//   k6 run -e STAGE=search scripts/loadtest/library.js         # searches only, ramping
//
// Other settings: BASE (API address), PEAK (users for spike/soak), DOWNLOADS=0 to skip the
// downloads (a slow home connection measures itself, not the server).
//
// This hits the LIVE server: run it when traffic is low. It stops by itself when more than
// 2% of requests fail or when searches get very slow (see thresholds).

import http from 'k6/http';
import { check, group, sleep } from 'k6';
import { Trend } from 'k6/metrics';

const BASE = __ENV.BASE || 'https://api.thaqalaynlibrary.com/api';
const STAGE = __ENV.STAGE || 'baseline';
const PEAK = Number(__ENV.PEAK || 100);
const DOWNLOADS = __ENV.DOWNLOADS !== '0';

// ── what is being searched ──────────────────────────────────────────────────────────
// Common words match hundreds of thousands of pages (the hardest case for ranking);
// rare ones a handful; phrases need every word on the same page.
const COMMON = ['الله', 'الصلاة', 'الحسين', 'علي', 'الإمام', 'رسول الله', 'القرآن', 'الجنة', 'الصبر',
  'الزكاة', 'الصوم', 'الحج', 'فاطمة', 'كربلاء', 'الولاية', 'التوحيد', 'الدعاء', 'العلم', 'الدنيا', 'الموت'];
const MEDIUM = ['زيارة عاشوراء', 'حديث الكساء', 'غدير خم', 'دعاء كميل', 'صلاة الليل', 'نهج البلاغة',
  'بيعة الرضوان', 'ليلة القدر', 'الخمس', 'التقية', 'الرجعة', 'البداء', 'المتعة', 'الشفاعة', 'العصمة'];
const RARE = ['الحبارى', 'القنبرة', 'الجندبادستر', 'السقمونيا', 'الطيلسان', 'الزعفران المسحوق', 'القرطاس',
  'الهليلج', 'المدر', 'الخطاف'];
const PHRASES = ['إنا لله وإنا إليه راجعون', 'لا حول ولا قوة إلا بالله', 'من كنت مولاه فعلي مولاه',
  'الحسن والحسين سيدا شباب أهل الجنة', 'إني تارك فيكم الثقلين', 'بسم الله الرحمن الرحيم',
  'أنا مدينة العلم وعلي بابها', 'حسين مني وأنا من حسين'];
const TOC_WORDS = ['الصبر', 'الصلاة', 'باب', 'فضل', 'الطهارة', 'الإمامة', 'الحج', 'التوبة', 'الدعاء',
  'مقدمة', 'الفهرس', 'الزكاة', 'التقليد', 'النكاح', 'المواريث'];

// ── how many users, for how long ────────────────────────────────────────────────────
const PLANS = {
  baseline: [{ duration: '1m', target: 5 }, { duration: '4m', target: 5 }],
  ramp: [
    { duration: '1m', target: 10 }, { duration: '4m', target: 10 },
    { duration: '1m', target: 25 }, { duration: '4m', target: 25 },
    { duration: '1m', target: 50 }, { duration: '4m', target: 50 },
    { duration: '1m', target: 100 }, { duration: '4m', target: 100 },
    { duration: '1m', target: 0 },
  ],
  spike: [{ duration: '10s', target: PEAK }, { duration: '2m', target: PEAK }, { duration: '30s', target: 0 }],
  soak: [{ duration: '2m', target: PEAK }, { duration: '30m', target: PEAK }, { duration: '1m', target: 0 }],
  search: [
    { duration: '1m', target: 10 }, { duration: '3m', target: 10 },
    { duration: '1m', target: 25 }, { duration: '3m', target: 25 },
    { duration: '1m', target: 50 }, { duration: '3m', target: 50 },
    { duration: '1m', target: 0 },
  ],
};

export const options = {
  scenarios: {
    readers: {
      executor: 'ramping-vus',
      stages: PLANS[STAGE] || PLANS.baseline,
      exec: STAGE === 'search' ? 'searcher' : 'reader',
      gracefulRampDown: '30s',
    },
  },
  thresholds: {
    // stop the test (not just fail it) before it hurts the real users
    http_req_failed: [{ threshold: 'rate<0.02', abortOnFail: true, delayAbortEval: '1m' }],
    'http_req_duration{kind:search}': [{ threshold: 'p(95)<15000', abortOnFail: true, delayAbortEval: '1m' }],
    // the goals, reported as pass/fail
    'search_ms{query:common}': ['p(95)<3000'],
    'search_ms{query:rare}': ['p(95)<1500'],
    'http_req_duration{kind:page}': ['p(95)<800'],
    'http_req_duration{kind:toc_search}': ['p(95)<2000'],
  },
  summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'max'],
  userAgent: 'thaqalayn-loadtest/1.0',
};

const searchMs = new Trend('search_ms', true);
const searchHits = new Trend('search_total_hits');

const pick = (list) => list[Math.floor(Math.random() * list.length)];
const think = (min, max) => sleep(min + Math.random() * (max - min));
const get = (path, kind, params = {}) =>
  http.get(`${BASE}${path}`, Object.assign({ tags: { kind, name: kind }, timeout: '60s' }, params));

// ── shared data, fetched once ───────────────────────────────────────────────────────
export function setup() {
  const books = [];
  for (const page of [1, 20, 60, 120, 180]) {  // books from across the catalog
    const r = http.get(`${BASE}/books?limit=100&page=${page}`, { timeout: '60s' });
    if (r.status === 200) {
      for (const b of r.json('items')) books.push({ id: b.bookId, pages: b.pageLast || 300 });
    }
  }
  const cats = http.get(`${BASE}/categories`, { timeout: '60s' });
  const subjects = cats.status === 200 ? cats.json().map((c) => c.id) : [];
  if (!books.length) throw new Error('could not read the catalog: is the API up?');
  return { books, subjects };
}

// ── one full-library search, as the app sends it ────────────────────────────────────
function search(data) {
  const roll = Math.random();
  let query, q;
  if (roll < 0.45) { query = 'common'; q = pick(COMMON); }
  else if (roll < 0.70) { query = 'medium'; q = pick(MEDIUM); }
  else if (roll < 0.85) { query = 'phrase'; q = pick(PHRASES); }
  else { query = 'rare'; q = pick(RARE); }
  let path = `/search?q=${encodeURIComponent(q)}&limit=20`;
  if (Math.random() < 0.2 && data.subjects.length) {  // some narrow it to a subject
    path += `&subject=${encodeURIComponent(pick(data.subjects))}`;
    query += '_filtered';
  }
  const r = get(path, 'search', { tags: { kind: 'search', name: 'search', query } });
  check(r, { 'search 200': (x) => x.status === 200 });
  searchMs.add(r.timings.duration, { query: query.replace('_filtered', '') });
  if (r.status === 200) searchHits.add(r.json('total') || 0);
  // a third of searchers look at the next page of results
  if (r.status === 200 && Math.random() < 0.33) {
    think(2, 5);
    const r2 = get(`${path}&page=2`, 'search', { tags: { kind: 'search', name: 'search_page2', query } });
    searchMs.add(r2.timings.duration, { query: 'page2' });
  }
}

function tocSearch() {
  const r = get(`/toc/search?q=${encodeURIComponent(pick(TOC_WORDS))}&limit=50`, 'toc_search');
  check(r, { 'toc search 200': (x) => x.status === 200 });
}

function read(data) {
  const book = pick(data.books);
  get(`/books/${book.id}`, 'book');
  get(`/books/${book.id}/toc`, 'toc');
  let seq = 1 + Math.floor(Math.random() * Math.max(1, book.pages - 10));
  const pages = 3 + Math.floor(Math.random() * 8);  // a reader turns a few pages
  for (let i = 0; i < pages; i++) {
    const r = get(`/books/${book.id}/pages/${seq + i}`, 'page');
    check(r, { 'page 200/404': (x) => x.status === 200 || x.status === 404 });
    think(3, 10);
  }
}

function browse() {
  get('/catalog/version', 'catalog');
  get(`/works?limit=50&page=${1 + Math.floor(Math.random() * 20)}`, 'works');
}

function download(data) {
  if (!DOWNLOADS) return;
  const book = pick(data.books);
  const r = get(`/books/${book.id}/download`, 'download',
    { responseType: 'none', headers: { 'Accept-Encoding': 'gzip' }, timeout: '180s' });
  check(r, { 'download 200': (x) => x.status === 200 });  // bytes: k6's own data_received
}

// ── a reader's session: mostly searching ────────────────────────────────────────────
export function reader(data) {
  const roll = Math.random();
  if (roll < 0.55) group('search', () => search(data));
  else if (roll < 0.65) group('toc search', () => tocSearch());
  else if (roll < 0.85) group('read', () => read(data));
  else if (roll < 0.95) group('browse', () => browse());
  else group('download', () => download(data));
  think(3, 12);
}

// ── searches only ───────────────────────────────────────────────────────────────────
export function searcher(data) {
  search(data);
  think(3, 10);
}
