// #118974 probe: can raw fs.watch on a desktop-plugins-shaped directory be
// driven into an event storm on this host, and does #127032's guardedWatch
// bound it? Run with Node >= 23 (strips TS types from the imported .ts).
//
// Per scenario and mode (raw | guarded) it reports: events delivered to JS,
// 'error' events, process CPU (user+system ms) and event-loop turns achieved,
// both DURING the filesystem operation and in a quiet 3 s window AFTER it
// (a real storm keeps going after the operation stops; normal churn does not).
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const { guardedWatch } = await import(pathToFileURL(process.env.GUARD_MODULE).href)

const sleep = ms => new Promise(r => setTimeout(r, ms))

function makeTree(root, plugins = 20) {
  fs.rmSync(root, { recursive: true, force: true })
  fs.mkdirSync(root, { recursive: true })
  for (let i = 0; i < plugins; i++) {
    const p = path.join(root, `plugin-${i}`)
    fs.mkdirSync(path.join(p, 'dist'), { recursive: true })
    fs.writeFileSync(path.join(p, 'manifest.json'), JSON.stringify({ id: `p${i}` }))
    for (let j = 0; j < 10; j++) fs.writeFileSync(path.join(p, 'dist', `f${j}.js`), 'x'.repeat(2000))
  }
}

async function timerLag(ms) {
  // 50 ms timers for `ms`; lag grows when the loop is flooded by callbacks
  const lags = []
  const end = Date.now() + ms
  while (Date.now() < end) {
    const t = Date.now()
    await sleep(50)
    lags.push(Date.now() - t - 50)
  }
  return { avg: Math.round(lags.reduce((a, b) => a + b, 0) / lags.length), max: Math.max(...lags) }
}

const scenarios = {
  // plugin reconcile: swap a plugin folder out and back, rapidly
  'child-dir-rename-churn': async root => {
    for (let i = 0; i < 300; i++) {
      const a = path.join(root, `plugin-${i % 20}`)
      fs.renameSync(a, a + '.tmp')
      fs.renameSync(a + '.tmp', a)
    }
  },
  // atomic file writes inside a plugin (write tmp + rename over)
  'atomic-file-writes': async root => {
    for (let i = 0; i < 1000; i++) {
      const f = path.join(root, `plugin-${i % 20}`, 'manifest.json')
      fs.writeFileSync(f + '.tmp', String(i))
      fs.renameSync(f + '.tmp', f)
    }
  },
  // the whole plugin set replaced by directory swap (dir -> .old, .new -> dir)
  'watched-dir-swap': async root => {
    makeTree(root + '.new', 20)
    fs.renameSync(root, root + '.old')
    fs.renameSync(root + '.new', root)
    fs.rmSync(root + '.old', { recursive: true, force: true })
  },
  // the watched directory is deleted out from under the watcher
  'watched-dir-deleted': async root => {
    fs.rmSync(root, { recursive: true, force: true })
  },
  // the watched directory is renamed away
  'watched-dir-renamed': async root => {
    fs.renameSync(root, root + '.moved')
  },
  // a plugin folder with many files deleted recursively
  'child-tree-delete': async root => {
    for (let i = 0; i < 20; i++) fs.rmSync(path.join(root, `plugin-${i}`), { recursive: true, force: true })
  }
}

async function runOne(name, op, mode) {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'fsw-'))
  const root = path.join(base, 'desktop-plugins')
  makeTree(root)
  let events = 0
  let errors = 0
  let pollChanges = 0
  let tripped = false
  const listener = () => { events++ }
  const rawWatch = l => {
    const w = fs.watch(root, l)
    w.on('error', () => { errors++ })
    return w
  }
  const handle =
    mode === 'raw'
      ? rawWatch(listener)
      : guardedWatch({
          platform: process.platform,
          watch: rawWatch,
          onEvent: listener,
          snapshot: () => fs.readdirSync(root).sort().join('\0'),
          onPollChange: () => { pollChanges++ },
          onTrip: () => { tripped = true }
        })

  await sleep(300)
  const cpu0 = process.cpuUsage()
  const t0 = Date.now()
  await op(root)
  await sleep(0)
  const opMs = Date.now() - t0
  const duringEvents = events
  const cpu1 = process.cpuUsage()
  const afterEvents0 = events
  const lag = await timerLag(3000)
  const cpu2 = process.cpuUsage(cpu1)
  const after = events - afterEvents0
  handle.close()
  const opCpu = process.cpuUsage(cpu0)
  try { fs.rmSync(base, { recursive: true, force: true }) } catch {}
  return {
    scenario: name, mode, opMs,
    eventsDuringOp: duringEvents,
    eventsIn3sAfter: after,
    eventsPerSecAfter: Math.round(after / 3),
    errors, tripped, pollChanges,
    cpuMsIn3sAfter: Math.round((cpu2.user + cpu2.system) / 1000),
    timerLagAvgMs: lag.avg, timerLagMaxMs: lag.max,
    cpuMsTotal: Math.round((opCpu.user + opCpu.system) / 1000)
  }
}

const rows = []
for (const [name, op] of Object.entries(scenarios)) {
  for (const mode of ['raw', 'guarded']) {
    for (let rep = 0; rep < 3; rep++) {
      try {
        rows.push({ rep, ...(await runOne(name, op, mode)) })
      } catch (e) {
        rows.push({ rep, scenario: name, mode, crash: String(e && e.stack || e).slice(0, 300) })
      }
      console.log(JSON.stringify(rows.at(-1)))
    }
  }
}
fs.writeFileSync(process.env.OUT || 'fswatch-results.json', JSON.stringify({ platform: process.platform, node: process.version, rows }, null, 2))
