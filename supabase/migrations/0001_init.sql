CREATE TABLE cooldowns (
	id SERIAL NOT NULL, 
	scope VARCHAR(20), 
	key VARCHAR(200), 
	until DATE, 
	reason TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_cooldown UNIQUE (scope, key)
);
CREATE TABLE learning (
	scope VARCHAR(20) NOT NULL, 
	key VARCHAR(200) NOT NULL, 
	useful INTEGER, 
	not_useful INTEGER, 
	clicks INTEGER, 
	ignored INTEGER, 
	PRIMARY KEY (scope, key)
);
CREATE TABLE offers (
	id SERIAL NOT NULL, 
	fingerprint VARCHAR(64) NOT NULL, 
	source VARCHAR(40), 
	title TEXT, 
	url TEXT, 
	retailer VARCHAR(120), 
	category VARCHAR(40), 
	price FLOAT, 
	was_price FLOAT, 
	data JSON, 
	seen_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (fingerprint)
);
CREATE INDEX ix_offers_seen_at ON offers (seen_at);
CREATE TABLE planned (
	id SERIAL NOT NULL, 
	raw TEXT NOT NULL, 
	query TEXT, 
	keywords JSON, 
	category VARCHAR(40), 
	target_price FLOAT, 
	max_price FLOAT, 
	url TEXT, 
	product_key VARCHAR(200), 
	details JSON, 
	status VARCHAR(20), 
	created_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id)
);
CREATE TABLE price_history (
	id SERIAL NOT NULL, 
	product_key VARCHAR(200) NOT NULL, 
	price FLOAT NOT NULL, 
	in_stock BOOLEAN, 
	observed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_price_history_product_key ON price_history (product_key);
CREATE INDEX ix_price_history_observed_at ON price_history (observed_at);
CREATE TABLE products (
	key VARCHAR(200) NOT NULL, 
	title TEXT, 
	url TEXT, 
	retailer VARCHAR(120), 
	category VARCHAR(40), 
	last_price FLOAT, 
	in_stock BOOLEAN, 
	last_checked TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (key)
);
CREATE TABLE purchases (
	id SERIAL NOT NULL, 
	date DATE NOT NULL, 
	retailer VARCHAR(120), 
	item TEXT, 
	category VARCHAR(40), 
	price FLOAT, 
	url TEXT, 
	product_key VARCHAR(200), 
	return_deadline DATE, 
	warranty_end DATE, 
	reminded JSON, 
	source_ref VARCHAR(200), 
	created_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (source_ref)
);
CREATE INDEX ix_purchases_product_key ON purchases (product_key);
CREATE TABLE state (
	key VARCHAR(100) NOT NULL, 
	value JSON, 
	PRIMARY KEY (key)
);
CREATE TABLE subscriptions (
	id SERIAL NOT NULL, 
	service VARCHAR(120), 
	category VARCHAR(40), 
	amount FLOAT, 
	previous_amount FLOAT, 
	cycle VARCHAR(20), 
	next_renewal DATE, 
	notes TEXT, 
	reminded_for DATE, 
	updated_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (service)
);
CREATE TABLE transactions (
	id SERIAL NOT NULL, 
	date DATE NOT NULL, 
	description TEXT NOT NULL, 
	retailer VARCHAR(120), 
	category VARCHAR(40), 
	amount FLOAT NOT NULL, 
	currency VARCHAR(3), 
	source VARCHAR(20), 
	ext_ref VARCHAR(200), 
	PRIMARY KEY (id), 
	UNIQUE (ext_ref)
);
CREATE INDEX ix_transactions_date ON transactions (date);
CREATE INDEX ix_transactions_category ON transactions (category);
CREATE INDEX ix_transactions_retailer ON transactions (retailer);
CREATE TABLE trips (
	id SERIAL NOT NULL, 
	kind VARCHAR(20), 
	provider VARCHAR(120), 
	name TEXT, 
	destination VARCHAR(120), 
	start_date DATE, 
	end_date DATE, 
	price FLOAT, 
	currency VARCHAR(3), 
	free_cancel_until DATE, 
	ref VARCHAR(120), 
	url TEXT, 
	reminded JSON, 
	source_ref VARCHAR(200), 
	PRIMARY KEY (id), 
	UNIQUE (source_ref)
);
CREATE INDEX ix_trips_start_date ON trips (start_date);
CREATE TABLE alerts (
	id SERIAL NOT NULL, 
	offer_id INTEGER, 
	kind VARCHAR(30), 
	tier VARCHAR(10), 
	verdict VARCHAR(10), 
	score FLOAT, 
	title TEXT, 
	retailer VARCHAR(120), 
	category VARCHAR(40), 
	est_saving FLOAT, 
	url TEXT, 
	payload JSON, 
	dedupe_key VARCHAR(200), 
	created_at TIMESTAMP WITH TIME ZONE, 
	sent_at TIMESTAMP WITH TIME ZONE, 
	clicked_at TIMESTAMP WITH TIME ZONE, 
	feedback VARCHAR(12), 
	acted BOOLEAN, 
	PRIMARY KEY (id), 
	FOREIGN KEY(offer_id) REFERENCES offers (id)
);
CREATE INDEX ix_alerts_tier ON alerts (tier);
CREATE INDEX ix_alerts_dedupe_key ON alerts (dedupe_key);
CREATE INDEX ix_alerts_created_at ON alerts (created_at);
CREATE TABLE alert_events (
	id SERIAL NOT NULL, 
	alert_id INTEGER, 
	event VARCHAR(20), 
	at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(alert_id) REFERENCES alerts (id)
);
CREATE INDEX ix_alert_events_alert_id ON alert_events (alert_id);
ALTER TABLE "cooldowns" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "learning" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "offers" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "planned" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "price_history" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "products" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "purchases" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "state" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "subscriptions" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "transactions" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "trips" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "alerts" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "alert_events" ENABLE ROW LEVEL SECURITY;ALTER TABLE alert_events ALTER COLUMN at SET DEFAULT now();
INSERT INTO state (key, value) VALUES ('link_secret', to_jsonb(md5(random()::text || clock_timestamp()::text) || md5(random()::text || clock_timestamp()::text))) ON CONFLICT (key) DO NOTHING;
