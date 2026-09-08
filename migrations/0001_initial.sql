-- Generated from src/models.py by scripts/generate_migration.py
CREATE TABLE reconciliation_runs (
	id INTEGER NOT NULL, 
	started_at DATETIME NOT NULL, 
	finished_at DATETIME, 
	status VARCHAR(20) NOT NULL, 
	triggered_by VARCHAR(64) NOT NULL, 
	matched_count INTEGER NOT NULL, 
	matched_within_tolerance_count INTEGER NOT NULL, 
	differs_count INTEGER NOT NULL, 
	unmatched_ledger_count INTEGER NOT NULL, 
	unmatched_statement_count INTEGER NOT NULL, 
	manually_resolved_count INTEGER NOT NULL, 
	cancelled_excluded_count INTEGER NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE source_files (
	id INTEGER NOT NULL, 
	filename VARCHAR(255) NOT NULL, 
	source_system VARCHAR(9) NOT NULL, 
	content_hash VARCHAR(64), 
	status VARCHAR(21) NOT NULL, 
	imported_at DATETIME NOT NULL, 
	row_count INTEGER NOT NULL, 
	rows_created INTEGER NOT NULL, 
	rows_corrected INTEGER NOT NULL, 
	rows_unchanged INTEGER NOT NULL, 
	rows_failed INTEGER NOT NULL, 
	is_correction BOOLEAN NOT NULL, 
	error_summary TEXT, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_source_files_content_hash ON source_files (content_hash);

CREATE TABLE transactions (
	id INTEGER NOT NULL, 
	source_system VARCHAR(9) NOT NULL, 
	natural_key VARCHAR(64) NOT NULL, 
	instrument VARCHAR(32) NOT NULL, 
	side VARCHAR(4) NOT NULL, 
	quantity NUMERIC(24, 8) NOT NULL, 
	price NUMERIC(24, 8) NOT NULL, 
	gross_amount NUMERIC(24, 8) NOT NULL, 
	transacted_at DATETIME NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	first_seen_file_id INTEGER NOT NULL, 
	current_version_id INTEGER, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_transaction_system_key UNIQUE (source_system, natural_key), 
	FOREIGN KEY(first_seen_file_id) REFERENCES source_files (id), 
	CONSTRAINT fk_current_version FOREIGN KEY(current_version_id) REFERENCES transaction_versions (id)
);

CREATE INDEX ix_transactions_natural_key ON transactions (natural_key);

CREATE TABLE manual_resolutions (
	id INTEGER NOT NULL, 
	resolution_type VARCHAR(21) NOT NULL, 
	ledger_transaction_id INTEGER, 
	statement_transaction_id INTEGER, 
	resolved_by VARCHAR(64) NOT NULL, 
	resolved_at DATETIME NOT NULL, 
	notes TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (ledger_transaction_id), 
	FOREIGN KEY(ledger_transaction_id) REFERENCES transactions (id), 
	UNIQUE (statement_transaction_id), 
	FOREIGN KEY(statement_transaction_id) REFERENCES transactions (id)
);

CREATE TABLE reconciliation_results (
	id INTEGER NOT NULL, 
	run_id INTEGER NOT NULL, 
	ledger_transaction_id INTEGER, 
	statement_transaction_id INTEGER, 
	match_status VARCHAR(24) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES reconciliation_runs (id), 
	FOREIGN KEY(ledger_transaction_id) REFERENCES transactions (id), 
	FOREIGN KEY(statement_transaction_id) REFERENCES transactions (id)
);

CREATE INDEX ix_reconciliation_results_run_id ON reconciliation_results (run_id);

CREATE TABLE transaction_versions (
	id INTEGER NOT NULL, 
	transaction_id INTEGER NOT NULL, 
	source_file_id INTEGER NOT NULL, 
	version_number INTEGER NOT NULL, 
	instrument VARCHAR(32) NOT NULL, 
	side VARCHAR(4) NOT NULL, 
	quantity NUMERIC(24, 8) NOT NULL, 
	price NUMERIC(24, 8) NOT NULL, 
	gross_amount NUMERIC(24, 8) NOT NULL, 
	transacted_at DATETIME NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	effective_from DATETIME NOT NULL, 
	effective_to DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_transaction_version UNIQUE (transaction_id, version_number), 
	FOREIGN KEY(transaction_id) REFERENCES transactions (id), 
	FOREIGN KEY(source_file_id) REFERENCES source_files (id)
);

CREATE INDEX ix_transaction_versions_transaction_id ON transaction_versions (transaction_id);

CREATE TABLE field_differences (
	id INTEGER NOT NULL, 
	result_id INTEGER NOT NULL, 
	field_name VARCHAR(32) NOT NULL, 
	ledger_value VARCHAR(64) NOT NULL, 
	statement_value VARCHAR(64) NOT NULL, 
	delta NUMERIC(24, 8), 
	is_significant BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(result_id) REFERENCES reconciliation_results (id)
);

CREATE INDEX ix_field_differences_result_id ON field_differences (result_id);
