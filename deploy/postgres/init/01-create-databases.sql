-- Runs once, on first start with an empty data volume.
-- Temporal's auto-setup image creates its own databases (temporal, temporal_visibility).
CREATE DATABASE claims;
CREATE DATABASE mock_core;
