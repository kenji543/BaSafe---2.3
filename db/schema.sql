PRAGMA foreign_keys = ON;

-- Basafe keeps only records needed by the Web-GIS assessment workflow.
-- Geometry is stored as RFC 7946 GeoJSON in EPSG:4326 so this schema works
-- with SQLite without requiring a spatial extension.

CREATE TABLE IF NOT EXISTS municipal_boundary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    geometry_geojson TEXT NOT NULL,
    source_metadata_json TEXT NOT NULL DEFAULT '{}',
    is_official INTEGER NOT NULL CHECK (is_official IN (0, 1)),
    is_demo INTEGER NOT NULL CHECK (is_demo IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (is_official + is_demo = 1)
);

CREATE TABLE IF NOT EXISTS barangays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    psgc_code TEXT,
    geometry_geojson TEXT NOT NULL,
    source_metadata_json TEXT NOT NULL DEFAULT '{}',
    is_official INTEGER NOT NULL CHECK (is_official IN (0, 1)),
    is_demo INTEGER NOT NULL CHECK (is_demo IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (is_official + is_demo = 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_barangays_psgc_code
    ON barangays(psgc_code)
    WHERE psgc_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_barangays_name ON barangays(name);

CREATE TABLE IF NOT EXISTS hazard_datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    hazard_type TEXT NOT NULL
        CHECK (hazard_type IN ('flood', 'liquefaction', 'ground_shaking')),
    source_name TEXT NOT NULL,
    source_date TEXT,
    quality_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (quality_status IN ('verified', 'provisional', 'limited', 'unknown')),
    is_official INTEGER NOT NULL CHECK (is_official IN (0, 1)),
    is_demo INTEGER NOT NULL CHECK (is_demo IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (is_official + is_demo = 1)
);

CREATE INDEX IF NOT EXISTS idx_hazard_datasets_type
    ON hazard_datasets(hazard_type);

CREATE TABLE IF NOT EXISTS hazard_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL REFERENCES hazard_datasets(id) ON DELETE CASCADE,
    classification TEXT NOT NULL,
    normalized_value REAL
        CHECK (normalized_value IS NULL OR
               (normalized_value >= 0.0 AND normalized_value <= 1.0)),
    geometry_geojson TEXT NOT NULL,
    properties_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_hazard_features_dataset
    ON hazard_features(dataset_id);
CREATE INDEX IF NOT EXISTS idx_hazard_features_classification
    ON hazard_features(classification);

CREATE TABLE IF NOT EXISTS historical_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_date TEXT,
    incident_type TEXT NOT NULL,
    severity TEXT,
    barangay_id INTEGER REFERENCES barangays(id) ON DELETE SET NULL,
    barangay TEXT,
    latitude REAL CHECK (latitude IS NULL OR
                         (latitude >= -90.0 AND latitude <= 90.0)),
    longitude REAL CHECK (longitude IS NULL OR
                          (longitude >= -180.0 AND longitude <= 180.0)),
    geometry_geojson TEXT,
    title TEXT NOT NULL,
    description TEXT,
    source_metadata_json TEXT NOT NULL DEFAULT '{}',
    is_official INTEGER NOT NULL CHECK (is_official IN (0, 1)),
    is_demo INTEGER NOT NULL CHECK (is_demo IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (is_official + is_demo = 1)
);

CREATE INDEX IF NOT EXISTS idx_incidents_date
    ON historical_incidents(incident_date);
CREATE INDEX IF NOT EXISTS idx_incidents_barangay
    ON historical_incidents(barangay_id, barangay);

CREATE TABLE IF NOT EXISTS clup_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    document_section TEXT,
    planning_note TEXT,
    barangay_id INTEGER REFERENCES barangays(id) ON DELETE SET NULL,
    geometry_geojson TEXT,
    source_metadata_json TEXT NOT NULL DEFAULT '{}',
    is_official INTEGER NOT NULL CHECK (is_official IN (0, 1)),
    is_demo INTEGER NOT NULL CHECK (is_demo IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (is_official + is_demo = 1)
);

CREATE INDEX IF NOT EXISTS idx_clup_reference_type
    ON clup_references(reference_type);
CREATE INDEX IF NOT EXISTS idx_clup_barangay
    ON clup_references(barangay_id);

CREATE TABLE IF NOT EXISTS fuzzy_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    defuzzification_method TEXT NOT NULL,
    output_min REAL NOT NULL DEFAULT 0.0,
    output_max REAL NOT NULL DEFAULT 100.0,
    validation_notes TEXT NOT NULL,
    configuration_json TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (output_max > output_min)
);

CREATE TABLE IF NOT EXISTS fuzzy_variables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER NOT NULL REFERENCES fuzzy_models(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    variable_kind TEXT NOT NULL
        CHECK (variable_kind IN ('input', 'output')),
    units TEXT,
    minimum_value REAL NOT NULL,
    maximum_value REAL NOT NULL,
    is_required INTEGER NOT NULL DEFAULT 1 CHECK (is_required IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0,
    configuration_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (model_id, slug),
    CHECK (maximum_value > minimum_value)
);

CREATE TABLE IF NOT EXISTS membership_functions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variable_id INTEGER NOT NULL REFERENCES fuzzy_variables(id) ON DELETE CASCADE,
    linguistic_label TEXT NOT NULL,
    function_type TEXT NOT NULL
        CHECK (function_type IN ('triangular', 'trapezoidal', 'gaussian', 'singleton')),
    parameters_json TEXT NOT NULL,
    output_value REAL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (variable_id, linguistic_label)
);

CREATE TABLE IF NOT EXISTS fuzzy_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER NOT NULL REFERENCES fuzzy_models(id) ON DELETE CASCADE,
    rule_code TEXT NOT NULL,
    rule_statement TEXT NOT NULL,
    antecedent_json TEXT NOT NULL,
    consequent_label TEXT NOT NULL,
    consequent_value REAL,
    weight REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0.0 AND weight <= 1.0),
    explanation TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (model_id, rule_code)
);

CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_token TEXT NOT NULL UNIQUE,
    model_id INTEGER NOT NULL REFERENCES fuzzy_models(id),
    barangay_id INTEGER REFERENCES barangays(id) ON DELETE SET NULL,
    location_label TEXT,
    selected_latitude REAL NOT NULL
        CHECK (selected_latitude >= -90.0 AND selected_latitude <= 90.0),
    selected_longitude REAL NOT NULL
        CHECK (selected_longitude >= -180.0 AND selected_longitude <= 180.0),
    status TEXT NOT NULL CHECK (status IN ('complete', 'incomplete')),
    incomplete_reason TEXT,
    source_snapshot_json TEXT NOT NULL DEFAULT '{}',
    recommendations_json TEXT NOT NULL DEFAULT '[]',
    disclaimer TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (status = 'complete' AND incomplete_reason IS NULL) OR
        (status = 'incomplete' AND incomplete_reason IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_assessments_created
    ON assessments(created_at);

CREATE TABLE IF NOT EXISTS assessment_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    variable_slug TEXT NOT NULL,
    classification TEXT,
    raw_value REAL,
    normalized_value REAL
        CHECK (normalized_value IS NULL OR
               (normalized_value >= 0.0 AND normalized_value <= 1.0)),
    availability_status TEXT NOT NULL
        CHECK (availability_status IN ('available', 'missing', 'not_applicable')),
    source_metadata_json TEXT NOT NULL DEFAULT '{}',
    quality_notice TEXT,
    UNIQUE (assessment_id, variable_slug)
);

CREATE TABLE IF NOT EXISTS assessment_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    variable_slug TEXT NOT NULL,
    linguistic_label TEXT NOT NULL,
    membership_value REAL NOT NULL
        CHECK (membership_value >= 0.0 AND membership_value <= 1.0),
    UNIQUE (assessment_id, variable_slug, linguistic_label)
);

CREATE TABLE IF NOT EXISTS assessment_rule_activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    rule_id INTEGER REFERENCES fuzzy_rules(id) ON DELETE SET NULL,
    rule_code TEXT NOT NULL,
    rule_statement TEXT NOT NULL,
    activation_strength REAL NOT NULL
        CHECK (activation_strength >= 0.0 AND activation_strength <= 1.0),
    weighted_strength REAL NOT NULL
        CHECK (weighted_strength >= 0.0 AND weighted_strength <= 1.0),
    consequent_label TEXT NOT NULL,
    contribution REAL,
    UNIQUE (assessment_id, rule_code)
);

CREATE INDEX IF NOT EXISTS idx_rule_activations_assessment_strength
    ON assessment_rule_activations(assessment_id, activation_strength DESC);

CREATE TABLE IF NOT EXISTS assessment_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id INTEGER NOT NULL UNIQUE
        REFERENCES assessments(id) ON DELETE CASCADE,
    final_score REAL
        CHECK (final_score IS NULL OR (final_score >= 0.0 AND final_score <= 100.0)),
    category TEXT,
    completeness_status TEXT NOT NULL
        CHECK (completeness_status IN ('complete', 'incomplete')),
    summary TEXT,
    data_quality_json TEXT NOT NULL DEFAULT '{}',
    calculated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (completeness_status = 'complete' AND final_score IS NOT NULL AND category IS NOT NULL) OR
        (completeness_status = 'incomplete' AND final_score IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS generated_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    report_format TEXT NOT NULL DEFAULT 'pdf'
        CHECK (report_format = 'pdf'),
    file_path TEXT,
    sha256 TEXT,
    content_snapshot_json TEXT NOT NULL DEFAULT '{}',
    generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_generated_reports_assessment
    ON generated_reports(assessment_id);

CREATE TABLE IF NOT EXISTS routing_study_areas (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    geometry_geojson TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_date TEXT,
    source_metadata_json TEXT NOT NULL DEFAULT '{}',
    is_official INTEGER NOT NULL DEFAULT 0 CHECK (is_official IN (0, 1)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_routing_study_areas_active
    ON routing_study_areas(is_active, is_official);

CREATE TABLE IF NOT EXISTS evacuation_centers (
    id INTEGER PRIMARY KEY,
    external_id TEXT,
    name TEXT NOT NULL,
    latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    barangay TEXT,
    designation TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_date TEXT,
    source_metadata_json TEXT NOT NULL DEFAULT '{}',
    dataset_version TEXT NOT NULL,
    is_official INTEGER NOT NULL DEFAULT 0 CHECK (is_official IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    capacity INTEGER CHECK (capacity IS NULL OR capacity >= 0),
    notes TEXT,
    photo_url TEXT,
    photo_alt TEXT,
    photo_source TEXT,
    photo_source_url TEXT,
    hazard_screening_status TEXT,
    hazard_score REAL CHECK (hazard_score IS NULL OR (hazard_score >= 0 AND hazard_score <= 100)),
    hazard_category TEXT,
    hazard_model_version TEXT,
    hazard_screened_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(external_id, dataset_version)
);

CREATE INDEX IF NOT EXISTS idx_evacuation_centers_active
    ON evacuation_centers(active);

CREATE TABLE IF NOT EXISTS searchable_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    result_type TEXT NOT NULL
        CHECK (result_type IN ('street', 'poi', 'place')),
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    alternate_name TEXT,
    normalized_alternate_name TEXT,
    barangay TEXT,
    latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    geometry_geojson TEXT,
    category TEXT,
    subtype TEXT,
    source_name TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    study_area_version TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, result_type, study_area_version)
);

CREATE INDEX IF NOT EXISTS idx_searchable_locations_name
    ON searchable_locations(active, normalized_name);
CREATE INDEX IF NOT EXISTS idx_searchable_locations_alternate
    ON searchable_locations(active, normalized_alternate_name);
CREATE INDEX IF NOT EXISTS idx_searchable_locations_type
    ON searchable_locations(active, result_type);

-- Isolated administration support. These tables contain privacy-preserving
-- aggregate activity and local administrative audit records only.
CREATE TABLE IF NOT EXISTS visitor_sessions (
    visitor_id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_path TEXT,
    page_views INTEGER NOT NULL DEFAULT 0 CHECK (page_views >= 0)
);

CREATE INDEX IF NOT EXISTS idx_visitor_sessions_last_seen
    ON visitor_sessions(last_seen);

CREATE TABLE IF NOT EXISTS visitor_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    visitor_id TEXT NOT NULL REFERENCES visitor_sessions(visitor_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL DEFAULT 'page_view' CHECK (event_type = 'page_view'),
    path TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_visitor_events_occurred
    ON visitor_events(occurred_at);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_log_created
    ON admin_audit_log(created_at DESC);
