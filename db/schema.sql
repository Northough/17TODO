-- 17TODO SQLite schema.
-- server/db.py 启动时 executescript 这个文件，全部语句都要可重复执行。

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  parent_id TEXT,
  gradient TEXT,                 -- JSON: ["#A3B4C2","#8B9DAD"]，为空表示跟随上级
  target_minutes INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS todos (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  task_id TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('loop', 'dated', 'open')),
  cycle_days INTEGER NOT NULL DEFAULT 0,
  on_expire TEXT NOT NULL DEFAULT 'reset' CHECK (on_expire IN ('reset', 'keep')),
  cycle_start TEXT,
  start_date TEXT,
  start_time TEXT,
  due_date TEXT,
  due_time TEXT,
  time_base INTEGER NOT NULL DEFAULT 0,
  done INTEGER NOT NULL DEFAULT 0,
  done_at INTEGER,
  done_at_was INTEGER,
  missed INTEGER NOT NULL DEFAULT 0,
  plan_id TEXT,
  plan_start TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- 专注片段。只由后端写入：计时落库、补记、自动结束。
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  todo_id TEXT,
  start_ts INTEGER NOT NULL,
  end_ts INTEGER NOT NULL,
  manual INTEGER NOT NULL DEFAULT 0,   -- 手动补记
  auto INTEGER NOT NULL DEFAULT 0,     -- 超时兜底自动结束
  created_at INTEGER NOT NULL
);

-- 当前计时。计时的唯一权威，页面关掉也在这儿。
CREATE TABLE IF NOT EXISTS running_timer (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  task_id TEXT,
  todo_id TEXT,
  start_ts INTEGER,                    -- NULL 表示暂停中
  accumulated_ms INTEGER NOT NULL DEFAULT 0,
  mode TEXT NOT NULL DEFAULT 'relax' CHECK (mode IN ('studying', 'paused', 'relax')),
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
  id TEXT PRIMARY KEY,
  todo_id TEXT,
  title TEXT NOT NULL,
  task_id TEXT,
  day TEXT NOT NULL,
  at_ts INTEGER NOT NULL,
  minutes INTEGER NOT NULL DEFAULT 0,
  from_day TEXT,
  cycle_days INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL CHECK (kind IN ('done', 'ended', 'expired', 'late_done')),
  mode TEXT NOT NULL DEFAULT 'open',
  range_text TEXT,
  overdue INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS periodic_plans (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  task_id TEXT,
  start_date TEXT NOT NULL,
  cycle_days INTEGER NOT NULL,
  due_days INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  last_created TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_task ON sessions(task_id);
CREATE INDEX IF NOT EXISTS idx_sessions_todo ON sessions(todo_id);
CREATE INDEX IF NOT EXISTS idx_todos_task ON todos(task_id);
CREATE INDEX IF NOT EXISTS idx_settlements_day ON settlements(day);
