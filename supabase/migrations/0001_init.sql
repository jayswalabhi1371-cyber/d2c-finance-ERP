-- D2C Finance OS — initial schema (generated from app/models.py; keep in sync).
-- Apply with: supabase db push   (or paste into the Supabase SQL editor)

-- Users <-> brands membership: drives row-level security.
create table if not exists brand_members (
    brand_id integer not null,
    user_id uuid not null references auth.users(id) on delete cascade,
    role text not null default 'viewer' check (role in ('owner','accountant','viewer')),
    primary key (brand_id, user_id)
);

CREATE TABLE brands (
	id SERIAL NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	gstin VARCHAR(15), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);


CREATE TABLE channels (
	id SERIAL NOT NULL, 
	brand_id INTEGER NOT NULL, 
	code VARCHAR(40) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	kind VARCHAR(20) NOT NULL, 
	commission_rate FLOAT NOT NULL, 
	fixed_fee FLOAT NOT NULL, 
	ship_rate_per_500g FLOAT NOT NULL, 
	settlement_days INTEGER NOT NULL, 
	claim_window_days INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (brand_id, code), 
	FOREIGN KEY(brand_id) REFERENCES brands (id)
);

CREATE INDEX ix_channels_brand_id ON channels (brand_id);

CREATE TABLE close_periods (
	id SERIAL NOT NULL, 
	brand_id INTEGER NOT NULL, 
	period VARCHAR(7) NOT NULL, 
	status VARCHAR(10) NOT NULL, 
	locked_at TIMESTAMP WITHOUT TIME ZONE, 
	snapshot JSON, 
	PRIMARY KEY (id), 
	UNIQUE (brand_id, period), 
	FOREIGN KEY(brand_id) REFERENCES brands (id)
);

CREATE INDEX ix_close_periods_brand_id ON close_periods (brand_id);

CREATE TABLE journal_entries (
	id SERIAL NOT NULL, 
	brand_id INTEGER NOT NULL, 
	entry_date DATE NOT NULL, 
	period VARCHAR(7) NOT NULL, 
	source VARCHAR(30) NOT NULL, 
	source_ref VARCHAR(120) NOT NULL, 
	narration VARCHAR(300) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(brand_id) REFERENCES brands (id)
);

CREATE INDEX ix_journal_entries_period ON journal_entries (period);
CREATE INDEX ix_journal_entries_entry_date ON journal_entries (entry_date);
CREATE INDEX ix_journal_entries_brand_id ON journal_entries (brand_id);

CREATE TABLE skus (
	id SERIAL NOT NULL, 
	brand_id INTEGER NOT NULL, 
	code VARCHAR(60) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	cogs FLOAT NOT NULL, 
	gst_rate FLOAT NOT NULL, 
	weight_g INTEGER NOT NULL, 
	opening_stock_qty INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (brand_id, code), 
	FOREIGN KEY(brand_id) REFERENCES brands (id)
);

CREATE INDEX ix_skus_brand_id ON skus (brand_id);

CREATE TABLE bank_deposits (
	id SERIAL NOT NULL, 
	brand_id INTEGER NOT NULL, 
	channel_id INTEGER NOT NULL, 
	settlement_id VARCHAR(80), 
	utr VARCHAR(40), 
	deposit_date DATE NOT NULL, 
	amount FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(brand_id) REFERENCES brands (id), 
	FOREIGN KEY(channel_id) REFERENCES channels (id)
);

CREATE INDEX ix_bank_deposits_channel_id ON bank_deposits (channel_id);
CREATE INDEX ix_bank_deposits_settlement_id ON bank_deposits (settlement_id);
CREATE INDEX ix_bank_deposits_brand_id ON bank_deposits (brand_id);

CREATE TABLE claims (
	id SERIAL NOT NULL, 
	brand_id INTEGER NOT NULL, 
	channel_id INTEGER NOT NULL, 
	rule VARCHAR(40) NOT NULL, 
	external_ref VARCHAR(120) NOT NULL, 
	description VARCHAR(400) NOT NULL, 
	expected FLOAT NOT NULL, 
	actual FLOAT NOT NULL, 
	leak_amount FLOAT NOT NULL, 
	event_date DATE NOT NULL, 
	deadline DATE NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	recovered_amount FLOAT NOT NULL, 
	recovered_date DATE, 
	ticket_ref VARCHAR(80), 
	notes VARCHAR(500), 
	detected_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (brand_id, rule, external_ref), 
	FOREIGN KEY(brand_id) REFERENCES brands (id), 
	FOREIGN KEY(channel_id) REFERENCES channels (id)
);

CREATE INDEX ix_claims_brand_id ON claims (brand_id);
CREATE INDEX ix_claims_channel_id ON claims (channel_id);

CREATE TABLE journal_lines (
	id SERIAL NOT NULL, 
	entry_id INTEGER NOT NULL, 
	brand_id INTEGER NOT NULL, 
	account VARCHAR(40) NOT NULL, 
	debit FLOAT NOT NULL, 
	credit FLOAT NOT NULL, 
	channel_code VARCHAR(40), 
	sku_code VARCHAR(60), 
	PRIMARY KEY (id), 
	FOREIGN KEY(entry_id) REFERENCES journal_entries (id), 
	FOREIGN KEY(brand_id) REFERENCES brands (id)
);

CREATE INDEX ix_journal_lines_account ON journal_lines (account);
CREATE INDEX ix_journal_lines_brand_id ON journal_lines (brand_id);
CREATE INDEX ix_journal_lines_entry_id ON journal_lines (entry_id);

CREATE TABLE logistics_shipments (
	id SERIAL NOT NULL, 
	brand_id INTEGER NOT NULL, 
	channel_id INTEGER NOT NULL, 
	awb VARCHAR(40) NOT NULL, 
	external_order_id VARCHAR(80) NOT NULL, 
	ship_date DATE NOT NULL, 
	payment_mode VARCHAR(10) NOT NULL, 
	cod_amount FLOAT NOT NULL, 
	remitted_amount FLOAT NOT NULL, 
	remittance_date DATE, 
	shipping_fee FLOAT NOT NULL, 
	charged_weight_g INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(brand_id) REFERENCES brands (id), 
	FOREIGN KEY(channel_id) REFERENCES channels (id)
);

CREATE INDEX ix_logistics_shipments_brand_id ON logistics_shipments (brand_id);
CREATE INDEX ix_logistics_shipments_channel_id ON logistics_shipments (channel_id);
CREATE INDEX ix_logistics_shipments_external_order_id ON logistics_shipments (external_order_id);

CREATE TABLE orders (
	id SERIAL NOT NULL, 
	brand_id INTEGER NOT NULL, 
	channel_id INTEGER NOT NULL, 
	external_id VARCHAR(80) NOT NULL, 
	order_date DATE NOT NULL, 
	delivered_date DATE, 
	sku_code VARCHAR(60) NOT NULL, 
	qty INTEGER NOT NULL, 
	gross_amount FLOAT NOT NULL, 
	payment_mode VARCHAR(10) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	declared_weight_g INTEGER, 
	logistics_code VARCHAR(40), 
	return_date DATE, 
	return_received BOOLEAN, 
	return_condition VARCHAR(20), 
	PRIMARY KEY (id), 
	UNIQUE (brand_id, channel_id, external_id), 
	FOREIGN KEY(brand_id) REFERENCES brands (id), 
	FOREIGN KEY(channel_id) REFERENCES channels (id)
);

CREATE INDEX ix_orders_external_id ON orders (external_id);
CREATE INDEX ix_orders_channel_id ON orders (channel_id);
CREATE INDEX ix_orders_brand_id ON orders (brand_id);

CREATE TABLE purchase_orders (
	id SERIAL NOT NULL, 
	brand_id INTEGER NOT NULL, 
	channel_id INTEGER NOT NULL, 
	po_number VARCHAR(60) NOT NULL, 
	po_date DATE NOT NULL, 
	expiry_date DATE, 
	stream VARCHAR(20) NOT NULL, 
	facility VARCHAR(80), 
	PRIMARY KEY (id), 
	UNIQUE (brand_id, channel_id, po_number), 
	FOREIGN KEY(brand_id) REFERENCES brands (id), 
	FOREIGN KEY(channel_id) REFERENCES channels (id)
);

CREATE INDEX ix_purchase_orders_channel_id ON purchase_orders (channel_id);
CREATE INDEX ix_purchase_orders_brand_id ON purchase_orders (brand_id);

CREATE TABLE qc_invoices (
	id SERIAL NOT NULL, 
	brand_id INTEGER NOT NULL, 
	channel_id INTEGER NOT NULL, 
	po_number VARCHAR(60) NOT NULL, 
	invoice_number VARCHAR(60) NOT NULL, 
	invoice_date DATE NOT NULL, 
	taxable_value FLOAT NOT NULL, 
	gst_amount FLOAT NOT NULL, 
	total FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(brand_id) REFERENCES brands (id), 
	FOREIGN KEY(channel_id) REFERENCES channels (id)
);

CREATE INDEX ix_qc_invoices_brand_id ON qc_invoices (brand_id);
CREATE INDEX ix_qc_invoices_invoice_number ON qc_invoices (invoice_number);
CREATE INDEX ix_qc_invoices_channel_id ON qc_invoices (channel_id);
CREATE INDEX ix_qc_invoices_po_number ON qc_invoices (po_number);

CREATE TABLE qc_payments (
	id SERIAL NOT NULL, 
	brand_id INTEGER NOT NULL, 
	channel_id INTEGER NOT NULL, 
	invoice_number VARCHAR(60) NOT NULL, 
	payment_date DATE NOT NULL, 
	invoice_amount FLOAT NOT NULL, 
	ad_deduction FLOAT NOT NULL, 
	tds_194q FLOAT NOT NULL, 
	other_deduction FLOAT NOT NULL, 
	net_paid FLOAT NOT NULL, 
	utr VARCHAR(40), 
	PRIMARY KEY (id), 
	FOREIGN KEY(brand_id) REFERENCES brands (id), 
	FOREIGN KEY(channel_id) REFERENCES channels (id)
);

CREATE INDEX ix_qc_payments_invoice_number ON qc_payments (invoice_number);
CREATE INDEX ix_qc_payments_channel_id ON qc_payments (channel_id);
CREATE INDEX ix_qc_payments_brand_id ON qc_payments (brand_id);

CREATE TABLE settlement_lines (
	id SERIAL NOT NULL, 
	brand_id INTEGER NOT NULL, 
	channel_id INTEGER NOT NULL, 
	settlement_id VARCHAR(80) NOT NULL, 
	external_order_id VARCHAR(80), 
	txn_type VARCHAR(20) NOT NULL, 
	posted_date DATE NOT NULL, 
	principal FLOAT NOT NULL, 
	commission FLOAT NOT NULL, 
	fixed_fee FLOAT NOT NULL, 
	shipping_fee FLOAT NOT NULL, 
	tcs FLOAT NOT NULL, 
	tds FLOAT NOT NULL, 
	other FLOAT NOT NULL, 
	net FLOAT NOT NULL, 
	charged_weight_g INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(brand_id) REFERENCES brands (id), 
	FOREIGN KEY(channel_id) REFERENCES channels (id)
);

CREATE INDEX ix_settlement_lines_channel_id ON settlement_lines (channel_id);
CREATE INDEX ix_settlement_lines_settlement_id ON settlement_lines (settlement_id);
CREATE INDEX ix_settlement_lines_external_order_id ON settlement_lines (external_order_id);
CREATE INDEX ix_settlement_lines_brand_id ON settlement_lines (brand_id);

CREATE TABLE tax_statements (
	id SERIAL NOT NULL, 
	brand_id INTEGER NOT NULL, 
	channel_id INTEGER NOT NULL, 
	period VARCHAR(7) NOT NULL, 
	section VARCHAR(10) NOT NULL, 
	amount FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(brand_id) REFERENCES brands (id), 
	FOREIGN KEY(channel_id) REFERENCES channels (id)
);

CREATE INDEX ix_tax_statements_brand_id ON tax_statements (brand_id);
CREATE INDEX ix_tax_statements_channel_id ON tax_statements (channel_id);

CREATE TABLE po_lines (
	id SERIAL NOT NULL, 
	po_id INTEGER NOT NULL, 
	sku_code VARCHAR(60) NOT NULL, 
	qty_ordered INTEGER NOT NULL, 
	qty_shipped INTEGER NOT NULL, 
	qty_received INTEGER NOT NULL, 
	qty_accepted INTEGER NOT NULL, 
	unit_price FLOAT NOT NULL, 
	grn_date DATE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(po_id) REFERENCES purchase_orders (id)
);

CREATE INDEX ix_po_lines_po_id ON po_lines (po_id);

alter table brand_members add foreign key (brand_id) references brands(id) on delete cascade;

-- ---------------------------------------------------------------------------
-- Row-level security: a user sees only brands they are a member of.
-- The FastAPI backend should connect with the user's JWT (or a service role that
-- sets request.jwt.claims) so these policies apply to every query.
-- ---------------------------------------------------------------------------
create or replace function is_brand_member(b integer) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from brand_members m where m.brand_id = b and m.user_id = auth.uid())
$$;
create or replace function can_write_brand(b integer) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from brand_members m where m.brand_id = b and m.user_id = auth.uid()
                 and m.role in ('owner','accountant'))
$$;

alter table brands enable row level security;
create policy brands_read on brands for select using (is_brand_member(id));
create policy brands_write on brands for update using (can_write_brand(id));

alter table brand_members enable row level security;
create policy members_read on brand_members for select using (user_id = auth.uid() or is_brand_member(brand_id));

alter table channels enable row level security;
create policy channels_read on channels for select using (is_brand_member(brand_id));
create policy channels_write on channels for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

alter table close_periods enable row level security;
create policy close_periods_read on close_periods for select using (is_brand_member(brand_id));
create policy close_periods_write on close_periods for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

alter table journal_entries enable row level security;
create policy journal_entries_read on journal_entries for select using (is_brand_member(brand_id));
create policy journal_entries_write on journal_entries for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

alter table skus enable row level security;
create policy skus_read on skus for select using (is_brand_member(brand_id));
create policy skus_write on skus for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

alter table bank_deposits enable row level security;
create policy bank_deposits_read on bank_deposits for select using (is_brand_member(brand_id));
create policy bank_deposits_write on bank_deposits for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

alter table claims enable row level security;
create policy claims_read on claims for select using (is_brand_member(brand_id));
create policy claims_write on claims for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

alter table journal_lines enable row level security;
create policy journal_lines_read on journal_lines for select using (is_brand_member(brand_id));
create policy journal_lines_write on journal_lines for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

alter table logistics_shipments enable row level security;
create policy logistics_shipments_read on logistics_shipments for select using (is_brand_member(brand_id));
create policy logistics_shipments_write on logistics_shipments for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

alter table orders enable row level security;
create policy orders_read on orders for select using (is_brand_member(brand_id));
create policy orders_write on orders for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

alter table purchase_orders enable row level security;
create policy purchase_orders_read on purchase_orders for select using (is_brand_member(brand_id));
create policy purchase_orders_write on purchase_orders for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

alter table qc_invoices enable row level security;
create policy qc_invoices_read on qc_invoices for select using (is_brand_member(brand_id));
create policy qc_invoices_write on qc_invoices for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

alter table qc_payments enable row level security;
create policy qc_payments_read on qc_payments for select using (is_brand_member(brand_id));
create policy qc_payments_write on qc_payments for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

alter table settlement_lines enable row level security;
create policy settlement_lines_read on settlement_lines for select using (is_brand_member(brand_id));
create policy settlement_lines_write on settlement_lines for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

alter table tax_statements enable row level security;
create policy tax_statements_read on tax_statements for select using (is_brand_member(brand_id));
create policy tax_statements_write on tax_statements for all using (can_write_brand(brand_id)) with check (can_write_brand(brand_id));

-- Child tables without brand_id inherit access through their parent.
alter table po_lines enable row level security;
create policy po_lines_read on po_lines for select using (exists (select 1 from purchase_orders p where p.id = po_id and is_brand_member(p.brand_id)));
create policy po_lines_write on po_lines for all using (exists (select 1 from purchase_orders p where p.id = po_id and can_write_brand(p.brand_id)));

-- Locked months are immutable at the database level too.
create or replace function block_locked_period() returns trigger language plpgsql as $$
begin
  if exists (select 1 from close_periods c where c.brand_id = coalesce(old.brand_id, new.brand_id)
             and c.period = coalesce(old.period, new.period) and c.status = 'locked') then
    raise exception 'Period % is locked', coalesce(old.period, new.period);
  end if;
  return coalesce(new, old);
end $$;
create trigger journal_entries_lock before insert or update or delete on journal_entries
  for each row execute function block_locked_period();
