"""Additive migration over the team's SQLite table names. Never reset a vault.

Original table contracts: bharaths27/VAYLT_Hackathon, db.py at 9849350.
All new metadata lives in cx_* tables. Legacy sources require explicit adoption.
"""
import sqlite3

BASE = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source(id TEXT PRIMARY KEY,title TEXT NOT NULL,kind TEXT NOT NULL,origin TEXT,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS source_chunk(id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES source(id),section TEXT,text TEXT NOT NULL,start_char INTEGER NOT NULL,end_char INTEGER NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS source_chunk_fts USING fts5(text,section,chunk_id UNINDEXED,tokenize='porter');
CREATE TABLE IF NOT EXISTS observation(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,source_id TEXT REFERENCES source(id),observed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS assertion(id TEXT PRIMARY KEY,subject TEXT NOT NULL,predicate TEXT NOT NULL,object TEXT NOT NULL,source_id TEXT REFERENCES source(id),chunk_id TEXT REFERENCES source_chunk(id),confidence REAL NOT NULL DEFAULT 1.0,supersedes TEXT REFERENCES assertion(id),created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS object_binding(object_id TEXT PRIMARY KEY,name TEXT NOT NULL,tag_id INTEGER UNIQUE,graspable INTEGER NOT NULL DEFAULT 1,width_mm REAL,last_zone TEXT,last_seen_at REAL,last_observation_id TEXT REFERENCES observation(id));
CREATE TABLE IF NOT EXISTS episode(id TEXT PRIMARY KEY,goal TEXT NOT NULL,request TEXT NOT NULL,plan_json TEXT,evidence_json TEXT,outcome TEXT NOT NULL DEFAULT 'proposed',failure TEXT,planner_calls INTEGER NOT NULL DEFAULT 0,planning_ms REAL,started_at REAL NOT NULL,finished_at REAL,skill_id TEXT);
CREATE TABLE IF NOT EXISTS operation_event(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL,episode_id TEXT REFERENCES episode(id),op TEXT NOT NULL,args_json TEXT NOT NULL,status TEXT NOT NULL,detail TEXT,at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS operation_event_op ON operation_event(operation_id);
CREATE TABLE IF NOT EXISTS skill_version(id TEXT PRIMARY KEY,name TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL,definition_json TEXT NOT NULL,digest TEXT NOT NULL,derived_from TEXT REFERENCES episode(id),created_at REAL NOT NULL,approved_at REAL,approved_by TEXT);
CREATE TABLE IF NOT EXISTS prospective_task(id TEXT PRIMARY KEY,trigger TEXT NOT NULL,goal TEXT NOT NULL,request TEXT NOT NULL,source_id TEXT REFERENCES source(id),status TEXT NOT NULL,created_at REAL NOT NULL,fired_at REAL,proposal_id TEXT);
CREATE TABLE IF NOT EXISTS negative_memory(id TEXT PRIMARY KEY,subject TEXT NOT NULL,constraint_text TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS approval(id TEXT PRIMARY KEY,target_kind TEXT NOT NULL,target_id TEXT NOT NULL,digest TEXT NOT NULL,approved_by TEXT NOT NULL,approved_at REAL NOT NULL,expires_at REAL NOT NULL,used_at REAL);
'''

ADDITIVE = '''
CREATE TABLE IF NOT EXISTS cx_source(source_id TEXT PRIMARY KEY REFERENCES source(id),workspace TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL,predecessor TEXT REFERENCES source(id),content_hash TEXT NOT NULL,cloud_ok INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cx_unresolved(source_id TEXT NOT NULL REFERENCES source(id),mentions_json TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cx_fact(assertion_id TEXT PRIMARY KEY REFERENCES assertion(id),fact_key TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cx_rule(assertion_id TEXT PRIMARY KEY REFERENCES assertion(id),rule_key TEXT NOT NULL,required_zone TEXT NOT NULL,status TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cx_rule_key ON cx_rule(rule_key,status);
CREATE TABLE IF NOT EXISTS cx_constraint(assertion_id TEXT PRIMARY KEY REFERENCES assertion(id),effect TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cx_entity(object_id TEXT PRIMARY KEY REFERENCES object_binding(object_id),aliases_json TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS cx_station(id TEXT PRIMARY KEY,name TEXT NOT NULL,aliases_json TEXT NOT NULL,tag_id INTEGER,version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS cx_goal(name TEXT PRIMARY KEY,goal_json TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES source(id),version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cx_derived(id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,payload_json TEXT NOT NULL,refs_json TEXT NOT NULL,basis_hash TEXT NOT NULL,fingerprint TEXT UNIQUE NOT NULL,origin TEXT NOT NULL,verifier TEXT,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cx_episode(id TEXT PRIMARY KEY REFERENCES episode(id),mode TEXT NOT NULL,goal_json TEXT NOT NULL,basis_hash TEXT NOT NULL,policy_hash TEXT NOT NULL,initial_json TEXT NOT NULL,request_key TEXT UNIQUE,input_hash TEXT NOT NULL,expires_at REAL NOT NULL,owner TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cx_episode_source(episode_id TEXT NOT NULL REFERENCES episode(id),source_id TEXT NOT NULL REFERENCES source(id),PRIMARY KEY(episode_id,source_id));
CREATE TABLE IF NOT EXISTS cx_skill(id TEXT PRIMARY KEY REFERENCES skill_version(id),goal_key TEXT NOT NULL,basis_hash TEXT NOT NULL,policy_hash TEXT NOT NULL,validation_json TEXT,mode TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cx_skill_goal ON cx_skill(goal_key);
CREATE TABLE IF NOT EXISTS cx_commitment(id TEXT PRIMARY KEY REFERENCES prospective_task(id),owner TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 1,expires_at REAL,source_revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS cx_due(task_id TEXT PRIMARY KEY REFERENCES prospective_task(id),due_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cx_consolidated(episode_id TEXT PRIMARY KEY REFERENCES episode(id),outcome TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cx_event(id TEXT PRIMARY KEY,trigger TEXT NOT NULL,payload_hash TEXT NOT NULL,origin TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cx_wake(event_id TEXT NOT NULL REFERENCES cx_event(id),task_id TEXT NOT NULL REFERENCES prospective_task(id),generation INTEGER NOT NULL,status TEXT NOT NULL,proposal_id TEXT,error TEXT,PRIMARY KEY(event_id,task_id,generation));
CREATE TABLE IF NOT EXISTS cx_decision(id TEXT PRIMARY KEY,stage TEXT NOT NULL,provider TEXT NOT NULL,model TEXT,accepted INTEGER NOT NULL,detail TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cx_practice(id TEXT PRIMARY KEY,case_json TEXT NOT NULL,expected_json TEXT NOT NULL,actual_json TEXT NOT NULL,status TEXT NOT NULL,verifier TEXT NOT NULL,mode TEXT NOT NULL DEFAULT 'SIMULATED',created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cx_dream(id TEXT PRIMARY KEY,started_at REAL NOT NULL,finished_at REAL,status TEXT NOT NULL,summary_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cx_manifest(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload_json TEXT NOT NULL,created_at REAL NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS cx_episode_fts USING fts5(request,episode_id UNINDEXED);
CREATE TRIGGER IF NOT EXISTS cx_episode_insert AFTER INSERT ON episode BEGIN INSERT INTO cx_episode_fts(request,episode_id) VALUES(new.request,new.id); END;
CREATE TABLE IF NOT EXISTS cx_schema(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL);
INSERT OR IGNORE INTO cx_schema VALUES(1,datetime('now'));
'''


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.executescript(BASE + ADDITIVE)
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES ('cortex_epoch','0')")
    conn.commit()
