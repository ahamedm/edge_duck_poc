/* edge_duck PoC front end — AlpineJS + duckdb-wasm.
 * The browser holds prior-year facts / scenario lines as Parquet buffers
 * registered into an in-browser DuckDB. All grid aggregation runs on the edge
 * engine; persistence always round-trips through the Flask API. */
'use strict';

const WASM_VERSION = '1.32.0';

function app() {
  return {
    /* ---- state ---- */
    tab: 'prior',
    error: '',
    calendars: [],
    calendarId: 1,
    refNodes: [],
    priorYearOptions: [2023, 2024, 2025],

    edge: { ready: false, loading: true, booting: false },
    edgeTables: [],

    prior: { level: 'SUBNOMINAL', lobId: '', ccId: '', years: [2023, 2024, 2025],
             viewYear: 2025, metric: 'ACTUAL', filter: '',
             loading: false, loaded: false, rows: [] },

    scenForm: { name: '', type: 'BUDGET', baseYear: 2025, seedLevel: 'SUBNOMINAL',
                seedVersion: 'ACTUAL', scopeId: '' },
    scenarios: [],
    currentScenario: null,
    scen: { viewYear: 0, years: [], rows: [] },
    staged: {},

    query: { engine: 'edge', sql: 'SELECT cc_code, version, SUM(amount) AS amount\nFROM prior_facts\nGROUP BY 1, 2', running: false, cols: [], rows: [], error: '' },

    /* ---- lifecycle ---- */
    async init() {
      try {
        const [cals, tree, scenList] = await Promise.all([
          fetch('/api/calendars').then(r => r.json()),
          fetch('/api/reference/tree').then(r => r.json()),
          fetch('/api/scenarios').then(r => r.json()),
        ]);
        this.calendars = cals;
        this.calendarId = cals[0] ? cals[0].id : 1;
        this.refNodes = tree;
        this.scenarios = scenList;
      } catch (e) { this.error = 'API unreachable: ' + e; }
      await this.initEdge();
      if (this.edge.ready) await this.loadRefIntoEdge();
    },

    async initEdge() {
      if (this.edge.booting || this.edge.ready) return;  // Alpine may re-enter
      this.edge.booting = true;
      try {
        const DUCK = await import(`https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@${WASM_VERSION}/+esm`);
        const bundle = await DUCK.selectBundle(DUCK.getJsDelivrBundles());
        // The worker bundle is a self-contained classic script; a blob worker must
        // load it via importScripts (cross-origin import() is not valid in a
        // classic worker and the engine then silently never initialises).
        const workerUrl = URL.createObjectURL(new Blob([`importScripts("${bundle.mainWorker}");`], { type: 'text/javascript' }));
        const worker = new Worker(workerUrl);
        this.ddb = new DUCK.AsyncDuckDB(new DUCK.ConsoleLogger(), worker);
        await this.ddb.instantiate(bundle.mainModule, bundle.pthreadWorker);
        await this.ddb.open({});
        this.conn = await this.ddb.connect();
        this.edge.ready = true;
      } catch (e) {
        this.error = 'Could not start duckdb-wasm (needs network access to jsdelivr): ' + e;
      } finally { this.edge.loading = false; }
    },

    async loadRefIntoEdge() {
      const values = this.refNodes.map(n =>
        `(${n.id}, '${n.node_type}', '${esc(n.code)}', '${esc(n.name)}', ${n.parent_id ?? 'NULL'}, '${esc(n.path)}')`).join(', ');
      await this.conn.query('DROP TABLE IF EXISTS ref_tree');
      await this.conn.query(`CREATE TABLE ref_tree AS SELECT * FROM (VALUES ${values}) AS t(id, node_type, code, name, parent_id, path)`);
      await this.refreshTableList();
    },

    /* ---- computed ---- */
    get lobs() { return this.refNodes.filter(n => n.node_type === 'LOB'); },
    get allCcs() { return this.refNodes.filter(n => n.node_type === 'COST_CENTRE'); },
    ccsOf(lobId) { return lobId ? this.allCcs.filter(c => c.parent_id === Number(lobId)) : []; },

    levelLabel(level) {
      return level === 'CC' ? 'Cost Centre'
        : level === 'NOMINAL' ? 'Cost Centre / Nominal'
        : 'Cost Centre / Nominal / Sub-Nominal';
    },

    resetLoaded() { this.prior.loaded = false; this.prior.rows = []; },

    /* ---- prior facts ---- */
    async loadPrior() {
      this.prior.loading = true; this.error = '';
      try {
        const params = new URLSearchParams({
          calendar: this.calendarId,
          years: this.prior.years.join(','),
          level: this.prior.level,
        });
        if (this.prior.lobId) params.set('lob', this.prior.lobId);
        if (this.prior.ccId) params.set('cost_centre', this.prior.ccId);
        const resp = await fetch('/api/facts/prior?' + params);
        if (!resp.ok) throw new Error(await errText(resp));
        await this.registerParquet('prior_facts', resp);
        if (!this.prior.years.includes(this.prior.viewYear)) this.prior.viewYear = this.prior.years[0];
        // Edge-side aggregation: prove the computation happens in the browser.
        const q = await this.edgeAggregate('prior_facts', this.prior.level);
        this.prior.rows = q;
        this.prior.loaded = true;
      } catch (e) { this.error = 'Load failed: ' + e; }
      finally { this.prior.loading = false; }
    },

    async registerParquet(name, resp) {
      const buf = new Uint8Array(await resp.arrayBuffer());
      // duckdb-wasm does not reliably replace an already-registered buffer under
      // the same name — register each fetch under a fresh file name instead.
      this.parquetSeq = (this.parquetSeq || 0) + 1;
      const fname = `${name}_v${this.parquetSeq}.parquet`;
      await this.ddb.registerFileBuffer(fname, buf);
      await this.conn.query(`DROP TABLE IF EXISTS ${name}`);
      await this.conn.query(`CREATE TABLE ${name} AS SELECT * FROM read_parquet('${fname}')`);
      await this.refreshTableList();
    },

    async edgeAggregate(table, level) {
      const keyCols = keyColsFor(level);
      const codeCols = codeColsFor(level);
      const nameCols = nameColsFor(level);
      const sel = [
        ...keyCols.map(c => `COALESCE(${c}, 0) AS ${c}`),
        ...codeCols, ...nameCols,
        'fiscal_year', 'period_num',
        'SUM(amount)::DOUBLE AS amount',
      ].join(', ');
      const group = [...keyCols, ...codeCols, ...nameCols, 'fiscal_year', 'period_num'].join(', ');
      const order = ['cc_code', ...codeCols.slice(1).map(c => `${c} NULLS LAST`)].join(', ');
      const res = await this.conn.query(
        `SELECT ${sel} FROM ${table} GROUP BY ${group} ORDER BY ${order}, fiscal_year, period_num`);
      return res.toArray().map(r => {
        const o = {};
        for (const k of Object.keys(r)) {
          const v = r[k];
          o[k] = typeof v === 'bigint' ? Number(v) : (v === null ? null : v);
        }
        return o;
      });
    },

    priorGrid() {
      return this.buildGrid(this.prior.rows, this.prior.level, this.prior.viewYear,
        this.prior.metric, this.prior.filter);
    },

    buildGrid(rows, level, viewYear, metric, filter) {
      const versioned = rows.length > 0 && rows[0].version !== undefined;
      const groups = new Map();
      for (const r of rows) {
        if (r.fiscal_year !== Number(viewYear)) continue;
        const label = nodeLabel(r, level);
        if (filter && !label.toLowerCase().includes(filter.toLowerCase())) continue;
        const key = keyColsFor(level).map(c => r[c]).join('|');
        if (!groups.has(key)) groups.set(key, { key, label, level, ids: key.split('|').map(Number), cells: {} });
        const g = groups.get(key);
        if (versioned) {
          g.cells[r.period_num] = g.cells[r.period_num] || {};
          g.cells[r.period_num][r.version] = r.amount;
        } else {
          g.cells[r.period_num] = r.amount;
        }
      }
      const out = [...groups.values()];
      for (const g of out) {
        g.total = 0;
        for (let p = 1; p <= 12; p++) {
          if (versioned) {
            const c = g.cells[p] || {};
            const b = c.BUDGET ?? null, a = c.ACTUAL ?? null;
            const v = metric === 'VARIANCE' ? (a !== null && b !== null ? a - b : null)
              : (c[metric] ?? null);
            g.cells[p] = v;
          }
          g.total += g.cells[p] || 0;
        }
      }
      out.sort((x, y) => x.label.localeCompare(y.label));
      return out;
    },

    /* ---- scenarios ---- */
    async createScenario() {
      const f = this.scenForm;
      if (!f.name) { this.error = 'Scenario name is required'; return; }
      const resp = await fetch('/api/scenarios', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: f.name, type: f.type, calendar_id: Number(this.calendarId),
          base_year: Number(f.baseYear), seed_level: f.seedLevel,
          seed_version: f.seedVersion,
          scope_node_id: f.scopeId ? Number(f.scopeId) : null,
        }),
      });
      if (!resp.ok) { this.error = 'Create failed: ' + await errText(resp); return; }
      const created = await resp.json();
      this.scenarios = await fetch('/api/scenarios').then(r => r.json());
      const s = this.scenarios.find(x => x.id === created.id);
      if (s) await this.openScenario(s);
    },

    async openScenario(s) {
      this.currentScenario = s;
      this.staged = {};
      this.scen.years = [s.base_year + 1, s.base_year + 2, s.base_year + 3];
      this.scen.viewYear = this.scen.years[0];
      const resp = await fetch(`/api/scenarios/${s.id}`);
      if (!resp.ok) { this.error = 'Open failed: ' + await errText(resp); return; }
      await this.registerParquet('scenario_lines', resp);
      this.scen.rows = await this.edgeAggregate('scenario_lines', s.seed_level);
      this.tab = 'scenarios';
    },

    closeScenario() { this.currentScenario = null; this.staged = {}; },

    scenarioGrid() {
      if (!this.currentScenario) return [];
      const rows = this.buildGrid(this.scen.rows, this.currentScenario.seed_level,
        this.scen.viewYear, 'BUDGET', '');   // scenario lines have no version column; BUDGET key unused
      for (const row of rows) {
        for (let p = 1; p <= 12; p++) {
          // cells hold the raw seeded amount here (metric logic above stored BUDGET-ish values)
          const k = this.editKey(row, p);
          row.cells[p] = k in this.staged ? this.staged[k].amount : row.cells[p];
          row.dirty = row.dirty || {};
          row.dirty[p] = k in this.staged;
        }
        row.total = Object.values(row.cells).reduce((a, v) => a + (v || 0), 0);
      }
      return rows;
    },

    editKey(row, p) { return `${row.ids.join('|')}|${this.scen.viewYear}|${p}`; },

    stageEdit(row, p, value) {
      const key = this.editKey(row, p);
      if (value === '') delete this.staged[key];
      else this.staged[key] = { ids: row.ids, fiscal_year: Number(this.scen.viewYear), period_num: p, amount: parseFloat(value) || 0 };
    },

    stagedCount() { return Object.keys(this.staged).length; },

    async saveScenario() {
      if (!this.currentScenario) return;
      const resp = await fetch(`/api/scenarios/${this.currentScenario.id}/lines`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lines: Object.values(this.staged).map(l => ({
          fiscal_year: l.fiscal_year, period_num: l.period_num,
          cost_centre_id: l.ids[0], nominal_id: l.ids[1] || 0, sub_nominal_id: l.ids[2] || 0,
          amount: l.amount,
        })) }),
      });
      if (!resp.ok) { this.error = 'Save failed: ' + await errText(resp); return; }
      const n = (await resp.json()).updated;
      this.staged = {};
      await this.openScenario(this.currentScenario);
      this.scenarios = await fetch('/api/scenarios').then(r => r.json());
      this.flash(`Saved ${n} cell(s) to the server DB`);
    },

    async setScenarioStatus(status) {
      if (!this.currentScenario) return;
      await fetch(`/api/scenarios/${this.currentScenario.id}/status`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      });
      this.currentScenario.status = status;
      this.scenarios = await fetch('/api/scenarios').then(r => r.json());
    },

    /* ---- query console ---- */
    async runQuery() {
      this.query.running = true; this.query.error = ''; this.query.cols = []; this.query.rows = [];
      try {
        if (this.query.engine === 'edge') {
          const res = await this.conn.query(this.query.sql);
          this.query.cols = res.schema.fields.map(f => f.name);
          // Arrow DECIMAL values surface unscaled (777.77 -> 77777); rescale via schema metadata
          const scales = res.schema.fields.map(f =>
            (f.type && typeof f.type.scale === 'number') ? f.type.scale : null);
          this.query.rows = res.toArray().map(row => {
            const o = {};
            for (let i = 0; i < this.query.cols.length; i++) {
              const v = row[this.query.cols[i]];
              let val = typeof v === 'bigint' ? Number(v) : (v === null || v === undefined ? null : v);
              if (scales[i] !== null && typeof val !== 'number') val = Number(String(v)) / Math.pow(10, scales[i]);
              o[this.query.cols[i]] = val;
            }
            return o;
          });
        } else {
          const resp = await fetch('/api/query', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sql: this.query.sql }),
          });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error || 'query failed');
          this.query.cols = data.cols;
          this.query.rows = data.rows.map(arr => Object.fromEntries(data.cols.map((c, i) => [c, arr[i]])));
        }
      } catch (e) { this.query.error = String(e); }
      finally { this.query.running = false; }
    },

    async refreshTableList() {
      try {
        const res = await this.conn.query(
          "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'");
        this.edgeTables = res.toArray().map(r => String(r.table_name));
      } catch { this.edgeTables = []; }
    },

    /* ---- display helpers ---- */
    fmt(v) {
      if (v === null || v === undefined) return '';
      return Number(v).toLocaleString('en-GB', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
    },
    fmtCell(v) { return v === null ? '' : (typeof v === 'number' ? this.fmt(v) : String(v)); },
    cellClass(v) {
      if (v === null || v === undefined) return '';
      return v < 0 ? 'neg' : '';
    },
    flash(msg) { this.error = ''; console.log(msg); },
  };
}

/* ---- module helpers ---- */
function keyColsFor(level) {
  if (level === 'CC') return ['cost_centre_id'];
  if (level === 'NOMINAL') return ['cost_centre_id', 'nominal_id'];
  return ['cost_centre_id', 'nominal_id', 'sub_nominal_id'];
}
function codeColsFor(level) {
  const c = ['cc_code'];
  if (level !== 'CC') c.push('nominal_code');
  if (level === 'SUBNOMINAL') c.push('sub_nominal_code');
  return c;
}
function nameColsFor(level) {
  const c = ['cc_name'];
  if (level !== 'CC') c.push('nominal_name');
  if (level === 'SUBNOMINAL') c.push('sub_nominal_name');
  return c;
}
function nodeLabel(r, level) {
  let label = `${r.cc_code} — ${r.cc_name}`;
  if (level !== 'CC') label += ' / ' + (r.nominal_code ? `${r.nominal_code} — ${r.nominal_name}` : '— Cost-Centre level —');
  if (level === 'SUBNOMINAL') label += ' / ' + (r.sub_nominal_code ? `${r.sub_nominal_code} — ${r.sub_nominal_name}` : (r.nominal_code ? '— Nominal level —' : '— Cost-Centre level —'));
  return label;
}
function esc(s) { return String(s).replace(/'/g, "''"); }
async function errText(resp) {
  try { const j = await resp.json(); return j.error || JSON.stringify(j); }
  catch { return `HTTP ${resp.status}`; }
}
window.app = app;
