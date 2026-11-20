-- kernel-kb schema.
--
-- Two kinds of tables live here:
--   * derived  : lane / node / edge / change / evaluation / measurement
--                Rebuilt from run facts on every `rebuild`. Never edit by hand.
--   * persisted: meta / technique
--                The vocabulary accumulates. `rebuild` only inserts, never deletes.
--
-- Rationale: facts are reproducible from run directories; vocabulary is curated
-- knowledge and must survive a rebuild.

PRAGMA journal_mode = WAL;

DROP VIEW  IF EXISTS v_tech_lift;
DROP VIEW  IF EXISTS v_champion;
DROP TABLE IF EXISTS measurement;
DROP TABLE IF EXISTS evaluation;
DROP TABLE IF EXISTS change;
DROP TABLE IF EXISTS edge;
DROP TABLE IF EXISTS node;
DROP TABLE IF EXISTS lane;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Controlled vocabulary of optimization techniques. Global, versioned.
-- status: draft (auto-derived) | stable (reviewed) | deprecated (merged away)
CREATE TABLE IF NOT EXISTS technique (
  technique_id      TEXT PRIMARY KEY,
  family            TEXT NOT NULL DEFAULT 'other',
  kind              TEXT,
  description       TEXT,
  aliases_json      TEXT NOT NULL DEFAULT '[]',
  status            TEXT NOT NULL DEFAULT 'draft',
  merged_into       TEXT,
  applies_when_json TEXT NOT NULL DEFAULT '{}',
  origin            TEXT NOT NULL DEFAULT 'draft'
);

-- Comparability domain. Every metric is only meaningful inside one lane.
CREATE TABLE lane (
  lane_id          TEXT PRIMARY KEY,
  operator         TEXT NOT NULL,
  dtype            TEXT,
  shape_bucket     TEXT,
  vendor           TEXT,
  device           TEXT,
  stack_json       TEXT,
  protocol_version TEXT
);

-- One row per immutable experiment node.
CREATE TABLE node (
  node_id           TEXT PRIMARY KEY,
  uid               TEXT,
  lane_id           TEXT,
  operator          TEXT NOT NULL,
  run_id            TEXT,
  generation        INTEGER,
  base_commit       TEXT,
  candidate_revision TEXT,
  tree_sha          TEXT,
  patch_sha         TEXT,
  patch_path        TEXT,
  hypothesis        TEXT,
  bottleneck_class  TEXT,
  status            TEXT,
  failure_kind      TEXT,
  failure_reason    TEXT,
  parent_ids_json   TEXT NOT NULL DEFAULT '[]',
  created_at        TEXT
);

CREATE INDEX IF NOT EXISTS node_lane ON node(lane_id, status);
CREATE INDEX IF NOT EXISTS node_op   ON node(operator, generation);

-- Code lineage only. Knowledge edges were deliberately dropped: the technique
-- vocabulary carries that meaning without needing agent-authored provenance.
CREATE TABLE edge (
  src  TEXT NOT NULL,
  dst  TEXT NOT NULL,
  type TEXT NOT NULL,
  PRIMARY KEY (src, dst, type)
);

CREATE INDEX IF NOT EXISTS edge_dst ON edge(dst, type);

-- Which technique a node applied. role=APPLIED when a patch was captured,
-- CONSIDERED when the node never produced one.
CREATE TABLE change (
  node_id      TEXT NOT NULL,
  technique_id TEXT NOT NULL,
  params_json  TEXT,
  role         TEXT,
  files_json   TEXT,
  note         TEXT,
  PRIMARY KEY (node_id, technique_id)
);

CREATE TABLE evaluation (
  evaluation_id            TEXT PRIMARY KEY,
  node_id                  TEXT NOT NULL,
  baseline_node_id         TEXT,
  protocol_version         TEXT,
  env_json                 TEXT,
  correctness              TEXT,
  correctness_cases        INTEGER,
  correctness_failures_json TEXT NOT NULL DEFAULT '[]',
  verdict                  TEXT,
  reasons_json             TEXT NOT NULL DEFAULT '[]',
  created_at               TEXT
);

-- One row per (evaluation, workload). The unit of analysis.
CREATE TABLE measurement (
  evaluation_id      TEXT NOT NULL,
  node_id            TEXT NOT NULL,
  workload_id        TEXT NOT NULL,
  role               TEXT,
  median_ms          REAL,
  baseline_median_ms REAL,
  speedup            REAL,
  noise              REAL,
  samples_json       TEXT,
  PRIMARY KEY (evaluation_id, workload_id)
);

CREATE INDEX IF NOT EXISTS measurement_node ON measurement(node_id, role);

-- technique x lane aggregate. The main entry point for mining.
CREATE VIEW v_tech_lift AS
SELECT c.technique_id,
       n.lane_id,
       l.operator,
       l.vendor,
       COUNT(DISTINCT m.node_id)                    AS n,
       ROUND(AVG(m.speedup), 4)                     AS avg_speedup,
       ROUND(AVG(CASE WHEN m.speedup >= 1.05 THEN 1.0 ELSE 0.0 END), 3) AS win_rate,
       GROUP_CONCAT(DISTINCT m.workload_id)         AS workloads
FROM change c
JOIN node        n ON n.node_id = c.node_id
JOIN lane        l ON l.lane_id = n.lane_id
JOIN measurement m ON m.node_id = c.node_id AND m.role = 'target'
WHERE c.role = 'APPLIED'
GROUP BY c.technique_id, n.lane_id, l.operator, l.vendor;

-- Current accepted node per lane, excluding superseded ones.
CREATE VIEW v_champion AS
SELECT n.lane_id,
       n.node_id,
       n.operator,
       n.generation,
       n.created_at,
       (SELECT MAX(m.speedup) FROM measurement m WHERE m.node_id = n.node_id) AS best_speedup
FROM node n
WHERE n.status = 'ACCEPTED'
  AND NOT EXISTS (
    SELECT 1 FROM edge e WHERE e.dst = n.node_id AND e.type = 'SUPERSEDES'
  );
