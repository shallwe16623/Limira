ALTER TABLE limira_artifact_events
    DROP CONSTRAINT IF EXISTS limira_artifact_events_artifact_type_check,
    DROP CONSTRAINT IF EXISTS chk_limira_artifact_events_artifact_type;

ALTER TABLE limira_artifact_events
    ADD CONSTRAINT chk_limira_artifact_events_artifact_type CHECK (
        artifact_type IN (
            'source_candidate',
            'retrieved_source',
            'evidence',
            'finding',
            'verified_claim',
            'entity',
            'relation',
            'timeline_event',
            'map_feature',
            'verification',
            'report_section'
        )
    );

ALTER TABLE limira_artifact_events
    DROP CONSTRAINT IF EXISTS limira_artifact_events_bucket_check,
    DROP CONSTRAINT IF EXISTS chk_limira_artifact_events_bucket;

ALTER TABLE limira_artifact_events
    ADD CONSTRAINT chk_limira_artifact_events_bucket CHECK (
        bucket IN (
            'source_candidates',
            'retrieved_sources',
            'evidence',
            'findings',
            'verified_claims',
            'entities',
            'relations',
            'timeline_events',
            'map_features',
            'verifications',
            'report_sections'
        )
    );
