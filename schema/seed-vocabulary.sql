-- ducklog: seed the vocabulary tables with umwelt's registered taxa and entity types.
-- Run after policy.sql creates the schema.

-- ============================================================================
-- Taxa (VSM-aligned, v0.5+)
-- ============================================================================

INSERT INTO taxa (name, canonical, vsm_system, description) VALUES
    ('principal',    NULL,          'S5',  'Commissioning identity'),
    ('world',        NULL,          'S0',  'Environment — what exists'),
    ('audit',        NULL,          'S3*', 'Cross-cut observer — outside the world'),
    ('state',        NULL,          'S3',  'What the harness tracks (hooks, budgets, jobs, modes)'),
    ('capability',   NULL,          'S1',  'What the actor can do (tools, kits, uses)'),
    ('actor',        NULL,          'S4',  'The four Ma actors'),
    -- VSM aliases (v0.5)
    ('control',      'state',       'S3',  'Alias: current-moment regulation'),
    ('coordination', 'state',       'S2',  'Alias: anti-oscillation, harness'),
    ('operation',    'capability',  'S1',  'Alias: tools, effects, uses'),
    ('intelligence', 'actor',       'S4',  'Alias: the inferencer');


-- ============================================================================
-- Entity types
-- ============================================================================

-- principal (S5)
INSERT INTO entity_types (taxon, name, category, description) VALUES
    ('principal', 'principal', 'identity', 'The commissioning principal');

-- world (S0)
INSERT INTO entity_types (taxon, name, parent_type, category, description) VALUES
    ('world', 'world',    NULL,    'environment',  'Named environment root'),
    ('world', 'mount',    'world', 'workspace',    'Bind mount or tmpfs'),
    ('world', 'dir',      NULL,    'filesystem',   'Directory'),
    ('world', 'file',     'dir',   'filesystem',   'File'),
    ('world', 'resource', NULL,    'budget',       'Runtime resource with a limit'),
    ('world', 'network',  NULL,    'network',      'Network endpoint'),
    ('world', 'env',      NULL,    'environment',  'Environment variable'),
    ('world', 'exec',     'world', 'executables',  'Executable binary in the jail');

-- capability / operation (S1)
INSERT INTO entity_types (taxon, name, category, description) VALUES
    ('capability', 'tool', 'tools',  'A callable tool'),
    ('capability', 'kit',  'tools',  'A named group of tools'),
    ('capability', 'use',  'access', 'Action-axis permissioned projection of a world resource');

-- state / control / coordination (S2 + S3)
INSERT INTO entity_types (taxon, name, category, description) VALUES
    ('state', 'hook',       'hooks',      'Lifecycle hook'),
    ('state', 'job',        'jobs',       'Execution run'),
    ('state', 'budget',     'budgets',    'Runtime budget'),
    ('state', 'mode',       'regulation', 'Regulation mode (S3)'),
    ('state', 'transition', 'coordination', 'Mode-change edge (S2)');

-- actor / intelligence (S4)
INSERT INTO entity_types (taxon, name, category, description) VALUES
    ('actor', 'inferencer', 'actors', 'The language model'),
    ('actor', 'executor',   'actors', 'A tool runner');

-- audit (S3*)
INSERT INTO entity_types (taxon, name, category, description) VALUES
    ('audit', 'observation', 'observation', 'Layer-2 observation entry'),
    ('audit', 'manifest',    'manifest',    'Workspace manifest reference');


-- ============================================================================
-- Property types (subset — the most commonly used ones)
-- ============================================================================

-- file properties (world-axis)
INSERT INTO property_types (taxon, entity_type, name, value_type, comparison, description) VALUES
    ('world', 'file', 'editable',  'bool', 'exact', 'Resource is editable (mount-level)'),
    ('world', 'file', 'visible',   'bool', 'exact', 'Resource is visible in the workspace'),
    ('world', 'file', 'show',      'str',  'exact', 'Projection: body, outline, signature');

-- tool properties (capability-axis)
INSERT INTO property_types (taxon, entity_type, name, value_type, comparison, description) VALUES
    ('capability', 'tool', 'allow',         'bool', 'exact',      'Tool is permitted'),
    ('capability', 'tool', 'visible',       'bool', 'exact',      'Tool is displayed to delegate'),
    ('capability', 'tool', 'max-level',     'int',  '<=',         'Max computation level'),
    ('capability', 'tool', 'allow-pattern', 'list', 'pattern-in', 'Glob patterns for allowed invocations'),
    ('capability', 'tool', 'deny-pattern',  'list', 'pattern-in', 'Glob patterns for denied invocations');

-- use properties (action-axis)
INSERT INTO property_types (taxon, entity_type, name, value_type, comparison, description) VALUES
    ('capability', 'use', 'editable',      'bool', 'exact',      'Access path grants edit'),
    ('capability', 'use', 'visible',       'bool', 'exact',      'Access path reveals resource'),
    ('capability', 'use', 'show',          'str',  'exact',      'Access path projection'),
    ('capability', 'use', 'allow',         'bool', 'exact',      'Access path permits invocation'),
    ('capability', 'use', 'deny',          'str',  'exact',      'Deny pattern for access path'),
    ('capability', 'use', 'allow-pattern', 'list', 'pattern-in', 'Glob patterns for access path'),
    ('capability', 'use', 'deny-pattern',  'list', 'pattern-in', 'Glob patterns denied via access path');

-- mode properties (control-axis)
INSERT INTO property_types (taxon, entity_type, name, value_type, comparison, description) VALUES
    ('state', 'mode', 'writable',    'str', 'exact', 'Comma-separated writable path prefixes'),
    ('state', 'mode', 'strategy',    'str', 'exact', 'Instruction injected when mode is active'),
    ('state', 'mode', 'description', 'str', 'exact', 'Human-readable mode description');

-- principal properties
INSERT INTO property_types (taxon, entity_type, name, value_type, comparison, description) VALUES
    ('principal', 'principal', 'intent', 'str', 'exact', 'Why this delegate was commissioned'),
    ('principal', 'principal', 'grade',  'int', 'exact', 'Ma-grade label (0-4)');

-- hook properties
INSERT INTO property_types (taxon, entity_type, name, value_type, comparison, description) VALUES
    ('state', 'hook', 'run',     'str', 'exact', 'Shell command to execute'),
    ('state', 'hook', 'timeout', 'str', 'exact', 'Timeout for hook execution');

-- transition properties
INSERT INTO property_types (taxon, entity_type, name, value_type, comparison, description) VALUES
    ('state', 'transition', 'run',       'str', 'exact', 'Command to run on transition'),
    ('state', 'transition', 'tier',      'int', 'exact', '1=advisory, 2=prepared, 3=auto-execute'),
    ('state', 'transition', 'phase',     'str', 'exact', 'before, after, on'),
    ('state', 'transition', 'condition', 'str', 'exact', 'Named runtime predicate'),
    ('state', 'transition', 'advisory',  'str', 'exact', 'Advisory message text'),
    ('state', 'transition', 'timeout',   'str', 'exact', 'Timeout for transition hook');

-- resource properties (world-axis)
INSERT INTO property_types (taxon, entity_type, name, value_type, comparison, description) VALUES
    ('world', 'resource', 'limit', 'str', 'exact', 'Resource limit with unit'),
    ('world', 'network',  'deny',  'str', 'exact', 'Deny pattern'),
    ('world', 'network',  'allow', 'bool', 'exact', 'Endpoint allowed'),
    ('world', 'env',      'allow', 'bool', 'exact', 'Env var passed through'),
    ('world', 'mount',    'readonly', 'bool', 'exact', 'Mount is read-only'),
    ('world', 'mount',    'source',   'str',  'exact', 'Host path'),
    ('world', 'mount',    'type',     'str',  'exact', 'Mount type: bind, tmpfs, overlay');

-- audit properties
INSERT INTO property_types (taxon, entity_type, name, value_type, comparison, description) VALUES
    ('audit', 'observation', 'source',  'str',  'exact', 'Observer source'),
    ('audit', 'observation', 'enabled', 'bool', 'exact', 'Whether observation is enabled'),
    ('audit', 'manifest',    'path',    'str',  'exact', 'Path to manifest file');
