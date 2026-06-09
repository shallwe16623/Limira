CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS limira_research_tasks (
    task_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    query TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
    ),
    archive_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        archive_status IN ('pending', 'ready', 'failed')
    ),
    scenario TEXT,
    runner_task_id TEXT,
    archive_object_key TEXT,
    archive_zip_sha256 TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error TEXT,
    model_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_limira_research_tasks_owner_created
    ON limira_research_tasks (owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_limira_research_tasks_status_created
    ON limira_research_tasks (status, created_at DESC);

CREATE TABLE IF NOT EXISTS limira_artifact_events (
    artifact_event_id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    task_id TEXT NOT NULL REFERENCES limira_research_tasks (task_id) ON DELETE CASCADE,
    local_artifact_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL CHECK (
        artifact_type IN (
            'evidence',
            'entity',
            'relation',
            'timeline_event',
            'map_feature',
            'verification',
            'report_section'
        )
    ),
    bucket TEXT NOT NULL CHECK (
        bucket IN (
            'evidence',
            'entities',
            'relations',
            'timeline_events',
            'map_features',
            'verifications',
            'report_sections'
        )
    ),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_refs TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    confidence NUMERIC(4, 3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    notes TEXT,
    source_event_type TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_limira_artifact_events_task_local
        UNIQUE (task_id, artifact_type, local_artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_limira_artifact_events_task_type_created
    ON limira_artifact_events (task_id, artifact_type, created_at);

CREATE TABLE IF NOT EXISTS limira_artifact_trace_events (
    trace_event_id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    task_id TEXT NOT NULL REFERENCES limira_research_tasks (task_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    artifact_type TEXT,
    bucket TEXT,
    local_artifact_id TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_event_type TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_limira_artifact_trace_events_task_created
    ON limira_artifact_trace_events (task_id, created_at);

CREATE TABLE IF NOT EXISTS limira_task_event_logs (
    event_log_id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    task_id TEXT NOT NULL REFERENCES limira_research_tasks (task_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'runner_stream',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_limira_task_event_logs_task_created
    ON limira_task_event_logs (task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS limira_evidence_items (
    evidence_storage_id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    evidence_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES limira_research_tasks (task_id) ON DELETE CASCADE,
    source_url TEXT,
    source_title TEXT,
    publisher TEXT,
    published_at TIMESTAMPTZ,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    original_text TEXT,
    translated_text TEXT,
    summary TEXT,
    language TEXT,
    credibility NUMERIC(4, 3) CHECK (credibility IS NULL OR credibility BETWEEN 0 AND 1),
    confidence NUMERIC(4, 3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    cross_verification JSONB NOT NULL DEFAULT '{}'::jsonb,
    conflict_notes TEXT,
    tool_name TEXT,
    model_name TEXT,
    human_confirmed BOOLEAN NOT NULL DEFAULT false,
    embedding vector(1536),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_limira_evidence_items_task_evidence
        UNIQUE (task_id, evidence_id)
);

CREATE INDEX IF NOT EXISTS idx_limira_evidence_items_task_collected
    ON limira_evidence_items (task_id, collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_limira_evidence_items_source_url
    ON limira_evidence_items (source_url);

CREATE INDEX IF NOT EXISTS idx_limira_evidence_items_embedding
    ON limira_evidence_items USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS limira_entities (
    entity_storage_id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    entity_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES limira_research_tasks (task_id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL CHECK (
        entity_type IN (
            'country',
            'agency',
            'company',
            'person',
            'policy',
            'bill',
            'sanction_target',
            'technology',
            'project',
            'location',
            'event'
        )
    ),
    display_name TEXT NOT NULL,
    canonical_name TEXT,
    country_code TEXT,
    geometry geometry(Geometry, 4326),
    confidence NUMERIC(4, 3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_limira_entities_task_entity UNIQUE (task_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_limira_entities_task_type_name
    ON limira_entities (task_id, entity_type, display_name);

CREATE INDEX IF NOT EXISTS idx_limira_entities_geometry
    ON limira_entities USING gist (geometry);

CREATE TABLE IF NOT EXISTS limira_entity_relations (
    relation_storage_id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    relation_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES limira_research_tasks (task_id) ON DELETE CASCADE,
    source_entity_id TEXT,
    target_entity_id TEXT,
    relation_type TEXT NOT NULL CHECK (
        relation_type IN (
            'sanctions',
            'regulates',
            'affects_industry',
            'owns',
            'partners_with',
            'located_in',
            'supply_chain_dependency',
            'mentions',
            'conflicts_with'
        )
    ),
    evidence_refs TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    confidence NUMERIC(4, 3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_limira_entity_relations_task_relation
        UNIQUE (task_id, relation_id),
    CONSTRAINT fk_limira_entity_relations_source_same_task
        FOREIGN KEY (task_id, source_entity_id)
        REFERENCES limira_entities (task_id, entity_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_limira_entity_relations_target_same_task
        FOREIGN KEY (task_id, target_entity_id)
        REFERENCES limira_entities (task_id, entity_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_limira_entity_relations_task_type
    ON limira_entity_relations (task_id, relation_type);

CREATE INDEX IF NOT EXISTS idx_limira_entity_relations_source_target
    ON limira_entity_relations (source_entity_id, target_entity_id);

CREATE TABLE IF NOT EXISTS limira_timeline_events (
    timeline_event_storage_id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    timeline_event_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES limira_research_tasks (task_id) ON DELETE CASCADE,
    event_title TEXT NOT NULL,
    event_type TEXT,
    event_time TIMESTAMPTZ,
    event_time_end TIMESTAMPTZ,
    location_name TEXT,
    geometry geometry(Geometry, 4326),
    risk_level TEXT NOT NULL DEFAULT 'unknown' CHECK (
        risk_level IN ('unknown', 'low', 'medium', 'high', 'critical')
    ),
    confidence NUMERIC(4, 3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    evidence_refs TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_limira_timeline_events_task_event
        UNIQUE (task_id, timeline_event_id)
);

CREATE INDEX IF NOT EXISTS idx_limira_timeline_events_task_time
    ON limira_timeline_events (task_id, event_time);

CREATE INDEX IF NOT EXISTS idx_limira_timeline_events_geometry
    ON limira_timeline_events USING gist (geometry);

CREATE TABLE IF NOT EXISTS limira_generated_reports (
    report_storage_id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    report_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES limira_research_tasks (task_id) ON DELETE CASCADE,
    report_type TEXT NOT NULL,
    markdown TEXT NOT NULL,
    html TEXT,
    pdf_object_key TEXT,
    evidence_refs TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    creator_user_id TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_limira_generated_reports_task_report
        UNIQUE (task_id, report_id)
);

CREATE INDEX IF NOT EXISTS idx_limira_generated_reports_task_created
    ON limira_generated_reports (task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS limira_uploaded_documents (
    document_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES limira_research_tasks (task_id) ON DELETE SET NULL,
    owner_user_id TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    content_type TEXT,
    byte_size BIGINT NOT NULL CHECK (byte_size >= 0),
    minio_bucket TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    extracted_text TEXT,
    language TEXT,
    embedding vector(1536),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_limira_uploaded_documents_owner_created
    ON limira_uploaded_documents (owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_limira_uploaded_documents_task
    ON limira_uploaded_documents (task_id);

CREATE INDEX IF NOT EXISTS idx_limira_uploaded_documents_embedding
    ON limira_uploaded_documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS limira_media_assets (
    asset_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES limira_research_tasks (task_id) ON DELETE SET NULL,
    owner_user_id TEXT NOT NULL,
    asset_type TEXT NOT NULL CHECK (
        asset_type IN ('audio', 'image', 'video', 'pdf', 'html', 'archive', 'other')
    ),
    minio_bucket TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    content_type TEXT,
    byte_size BIGINT CHECK (byte_size IS NULL OR byte_size >= 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_limira_media_assets_owner_created
    ON limira_media_assets (owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_limira_media_assets_task
    ON limira_media_assets (task_id);
