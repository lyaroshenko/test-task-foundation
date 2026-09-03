-- =============================================================================
-- Donor Data Platform — схема даних
-- SQLite (діалект свідомо простий, легко переноситься на Postgres)
--
-- Три шари:
--   RAW      — те, що прийшло з джерела, незмінне
--   CORE     — канонічна донорська модель (CRM)
--   DERIVED  — розраховані показники та сегменти
--   OPS      — службові таблиці для контролю за автоматизацією
-- =============================================================================

PRAGMA foreign_keys = ON;


-- =============================================================================
-- ДОВІДНИКИ
-- =============================================================================

-- Канал надходження пожертви. Не плутати з кампанією: канал — це "як гроші
-- фізично прийшли", кампанія — "у відповідь на що людина дала".
CREATE TABLE source (
    source_id           TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    source_type         TEXT NOT NULL
                        CHECK (source_type IN ('website','paypal','bank_transfer','check','crypto','cash','other')),
    ingestion_mode      TEXT NOT NULL
                        CHECK (ingestion_mode IN ('api_webhook','file_import','manual_entry')),
    default_currency    TEXT,
    -- Комісійна модель каналу: потрібна, щоб рахувати net, коли джерело
    -- не віддає суму комісії явно (типово для банківських переказів).
    fee_percent         REAL DEFAULT 0,
    fee_fixed           REAL DEFAULT 0,
    is_active           INTEGER NOT NULL DEFAULT 1
);

-- Кампанія / appeal — привід для звернення. Дозволяє рахувати
-- ефективність конкретних збірок, а не тільки каналів.
CREATE TABLE campaign (
    campaign_id         TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    campaign_type       TEXT CHECK (campaign_type IN ('emergency','annual','project','major_gift','recurring_drive','other')),
    started_on          DATE,
    ended_on            DATE,
    goal_amount_base    REAL,
    is_active           INTEGER NOT NULL DEFAULT 1
);

-- Призначення коштів. Для фонду критично: обмежені (restricted) кошти
-- не можна витрачати на інші програми, і в звітності вони йдуть окремо.
CREATE TABLE fund (
    fund_id             TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    is_restricted       INTEGER NOT NULL DEFAULT 0
);

-- Курси валют на дату. Без цієї таблиці мультивалютна аналітика
-- перестає сходитись при кожному перерахунку заднім числом.
CREATE TABLE fx_rate (
    rate_date           DATE NOT NULL,
    currency            TEXT NOT NULL,
    rate_to_base        REAL NOT NULL,       -- скільки базової валюти за 1 одиницю currency
    source              TEXT DEFAULT 'NBU',
    PRIMARY KEY (rate_date, currency)
);


-- =============================================================================
-- ШАР RAW — незмінний журнал того, що прийшло
-- =============================================================================

-- Пачка завантаження: один вебхук, один CSV-файл, одна виписка.
-- Контрольні суми тут потрібні для звірки (див. ops_reconciliation).
CREATE TABLE ingestion_batch (
    batch_id            TEXT PRIMARY KEY,
    source_id           TEXT NOT NULL REFERENCES source(source_id),
    file_name           TEXT,
    file_hash           TEXT,                -- захист від повторного завантаження того самого файлу
    received_at         TIMESTAMP NOT NULL,
    declared_row_count  INTEGER,             -- скільки рядків заявляє джерело
    declared_total      REAL,                -- контрольна сума з джерела
    status              TEXT NOT NULL DEFAULT 'received'
                        CHECK (status IN ('received','parsed','loaded','failed','reconciled')),
    error_message       TEXT
);

-- Сирий рядок як прийшов. Ніколи не редагується і не видаляється:
-- будь-яку помилку обробки можна переграти з цього шару.
CREATE TABLE raw_donation (
    raw_id              TEXT PRIMARY KEY,
    batch_id            TEXT NOT NULL REFERENCES ingestion_batch(batch_id),
    source_id           TEXT NOT NULL REFERENCES source(source_id),
    external_id         TEXT,                -- id транзакції у джерелі
    payload             TEXT NOT NULL,       -- оригінальний JSON / рядок CSV
    row_hash            TEXT NOT NULL,       -- хеш payload: ловить дублі там, де external_id відсутній
    received_at         TIMESTAMP NOT NULL,
    processing_status   TEXT NOT NULL DEFAULT 'pending'
                        CHECK (processing_status IN ('pending','processed','duplicate','review','rejected','failed')),
    processed_at        TIMESTAMP,
    reject_reason       TEXT
);

-- Ключ ідемпотентності. Друге надходження тієї самої транзакції
-- (ретрай вебхука, повторний імпорт виписки) відсікається тут,
-- а не після того, як річна сума збору вже подвоїлась.
CREATE UNIQUE INDEX ux_raw_source_external
    ON raw_donation(source_id, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX ix_raw_hash   ON raw_donation(source_id, row_hash);
CREATE INDEX ix_raw_status ON raw_donation(processing_status);


-- =============================================================================
-- ШАР CORE — канонічна модель
-- =============================================================================

CREATE TABLE donor (
    donor_id            TEXT PRIMARY KEY,
    donor_type          TEXT NOT NULL DEFAULT 'individual'
                        CHECK (donor_type IN ('individual','organization','anonymous')),

    first_name          TEXT,
    last_name           TEXT,
    org_name            TEXT,
    display_name        TEXT NOT NULL,
    -- Нормалізована латинська форма імені. Дозволяє зіставити
    -- "Ковальчук Олена" з банківської виписки та "Olena Kovalchuk" з PayPal.
    name_normalized     TEXT,

    country             TEXT,
    city                TEXT,
    address_line        TEXT,
    postal_code         TEXT,
    preferred_language  TEXT DEFAULT 'uk',

    donor_status        TEXT NOT NULL DEFAULT 'active'
                        CHECK (donor_status IN ('active','lapsed','do_not_contact','deceased','merged')),
    -- Позначає донора як великого: для нього автоматичні листи вимикаються,
    -- натомість створюється задача менеджеру.
    is_major_prospect   INTEGER NOT NULL DEFAULT 0,

    -- М'який мердж: запис лишається, посилання веде на переможця.
    merged_into_donor_id TEXT REFERENCES donor(donor_id),

    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL,
    created_by          TEXT DEFAULT 'system',
    data_quality_flags  TEXT                 -- JSON: {"missing_email": true, ...}
);

CREATE INDEX ix_donor_name   ON donor(name_normalized);
CREATE INDEX ix_donor_status ON donor(donor_status);
CREATE INDEX ix_donor_merged ON donor(merged_into_donor_id);

-- Усі ідентифікатори донора в одній таблиці.
-- Головна причина: одна людина = багато ключів (2 email, телефон, PayPal id,
-- хеш IBAN). Матчинг стає пошуком по одному індексу замість каскаду OR.
CREATE TABLE donor_identifier (
    identifier_id       TEXT PRIMARY KEY,
    donor_id            TEXT NOT NULL REFERENCES donor(donor_id),
    id_type             TEXT NOT NULL
                        CHECK (id_type IN ('email','phone','paypal_payer_id','bank_account_hash','stripe_customer_id','tax_id','external_crm_id')),
    id_value            TEXT NOT NULL,       -- як прийшло
    id_value_normalized TEXT NOT NULL,       -- lowercase, E.164, gmail без крапок і +тегів
    is_primary          INTEGER NOT NULL DEFAULT 0,
    is_verified         INTEGER NOT NULL DEFAULT 0,
    first_seen_at       TIMESTAMP NOT NULL,
    last_seen_at        TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX ux_identifier ON donor_identifier(id_type, id_value_normalized, donor_id);
CREATE INDEX ix_identifier_lookup ON donor_identifier(id_type, id_value_normalized);

-- Регулярна підписка — окрема сутність від транзакцій.
-- Транзакції можуть падати (прострочена картка), а підписка при цьому жива.
CREATE TABLE recurring_plan (
    plan_id             TEXT PRIMARY KEY,
    donor_id            TEXT NOT NULL REFERENCES donor(donor_id),
    source_id           TEXT NOT NULL REFERENCES source(source_id),
    external_subscription_id TEXT,
    amount              REAL NOT NULL,
    currency            TEXT NOT NULL,
    frequency           TEXT NOT NULL CHECK (frequency IN ('monthly','quarterly','annual')),
    started_on          DATE NOT NULL,
    cancelled_on        DATE,
    cancel_reason       TEXT,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','paused','failing','cancelled')),
    failed_attempts     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX ix_plan_donor ON recurring_plan(donor_id, status);

CREATE TABLE donation (
    donation_id         TEXT PRIMARY KEY,
    donor_id            TEXT NOT NULL REFERENCES donor(donor_id),
    raw_id              TEXT REFERENCES raw_donation(raw_id),
    source_id           TEXT NOT NULL REFERENCES source(source_id),
    campaign_id         TEXT REFERENCES campaign(campaign_id),
    fund_id             TEXT REFERENCES fund(fund_id),
    plan_id             TEXT REFERENCES recurring_plan(plan_id),

    external_transaction_id TEXT,
    donated_at          TIMESTAMP NOT NULL,

    -- Гроші зберігаємо в трьох вимірах: оригінал, курс, база.
    amount_original     REAL NOT NULL,
    currency            TEXT NOT NULL,
    fx_rate             REAL NOT NULL DEFAULT 1.0,
    amount_base         REAL NOT NULL,       -- amount_original * fx_rate
    fee_base            REAL NOT NULL DEFAULT 0,
    amount_net_base     REAL NOT NULL,       -- скільки реально дійшло до фонду

    payment_method      TEXT,
    donation_status     TEXT NOT NULL DEFAULT 'completed'
                        CHECK (donation_status IN ('completed','pending','failed','refunded','chargeback')),
    -- Повернення зберігаємо окремим записом з від'ємною сумою і посиланням
    -- на оригінал: історія лишається чесною, а SUM() не бреше.
    refund_of_donation_id TEXT REFERENCES donation(donation_id),

    is_recurring        INTEGER NOT NULL DEFAULT 0,
    is_anonymous        INTEGER NOT NULL DEFAULT 0,
    -- Порядковий номер пожертви цього донора. Дає миттєву відповідь
    -- на питання "новий чи повторний" без віконних функцій у кожному запиті.
    donation_sequence   INTEGER,

    -- Аудит матчингу: як саме ми вирішили, що це цей донор.
    match_method        TEXT CHECK (match_method IN ('exact_email','exact_phone','exact_processor_id','bank_account','fuzzy_name','manual','new_donor')),
    match_confidence    REAL,

    created_at          TIMESTAMP NOT NULL,
    notes               TEXT
);

CREATE UNIQUE INDEX ux_donation_external
    ON donation(source_id, external_transaction_id) WHERE external_transaction_id IS NOT NULL;
CREATE INDEX ix_donation_donor ON donation(donor_id, donated_at);
CREATE INDEX ix_donation_date  ON donation(donated_at);


-- =============================================================================
-- КОМУНІКАЦІЇ ТА ЗГОДИ
-- =============================================================================

-- Без цієї таблиці не можна законно сформувати розсилку.
-- Донори з ЄС і США — це GDPR і CAN-SPAM, а не питання ввічливості.
CREATE TABLE donor_consent (
    consent_id          TEXT PRIMARY KEY,
    donor_id            TEXT NOT NULL REFERENCES donor(donor_id),
    consent_type        TEXT NOT NULL
                        CHECK (consent_type IN ('email_marketing','newsletter','phone_contact','postal_mail','data_processing')),
    status              TEXT NOT NULL CHECK (status IN ('granted','revoked','never_asked')),
    legal_basis         TEXT CHECK (legal_basis IN ('consent','legitimate_interest','contract')),
    granted_at          TIMESTAMP,
    revoked_at          TIMESTAMP,
    consent_source      TEXT                 -- де саме людина поставила галочку
);

CREATE INDEX ix_consent_donor ON donor_consent(donor_id, consent_type, status);

CREATE TABLE communication (
    communication_id    TEXT PRIMARY KEY,
    donor_id            TEXT NOT NULL REFERENCES donor(donor_id),
    donation_id         TEXT REFERENCES donation(donation_id),
    campaign_id         TEXT REFERENCES campaign(campaign_id),

    comm_type           TEXT NOT NULL
                        CHECK (comm_type IN ('receipt','thank_you','welcome','newsletter','ask','impact_report','reactivation','recurring_dunning','call','meeting')),
    channel             TEXT NOT NULL CHECK (channel IN ('email','phone','postal','sms','in_person')),
    direction           TEXT NOT NULL DEFAULT 'outbound' CHECK (direction IN ('outbound','inbound')),

    scheduled_at        TIMESTAMP,
    sent_at             TIMESTAMP,
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','sent','delivered','opened','clicked','bounced','failed','skipped')),
    skip_reason         TEXT,                -- напр. "no consent", "major donor — manual"
    template_code       TEXT,
    is_automated        INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX ix_comm_donor ON communication(donor_id, sent_at);
CREATE INDEX ix_comm_type  ON communication(comm_type, status);


-- =============================================================================
-- ШАР DERIVED — розраховані показники
-- =============================================================================

-- Матеріалізовані метрики донора. Оновлюються інкрементально після
-- кожної пожертви, а не перераховуються по всій базі.
CREATE TABLE donor_metrics (
    donor_id                TEXT PRIMARY KEY REFERENCES donor(donor_id),

    first_donation_at       TIMESTAMP,
    last_donation_at        TIMESTAMP,
    donation_count          INTEGER NOT NULL DEFAULT 0,
    lifetime_amount_base    REAL NOT NULL DEFAULT 0,
    lifetime_net_base       REAL NOT NULL DEFAULT 0,
    avg_gift_base           REAL,
    largest_gift_base       REAL,
    days_since_last_gift    INTEGER,

    has_active_recurring    INTEGER NOT NULL DEFAULT 0,
    recurring_amount_base   REAL DEFAULT 0,
    distinct_years_given    INTEGER NOT NULL DEFAULT 0,
    consecutive_years_given INTEGER NOT NULL DEFAULT 0,

    -- RFM: класична модель сегментації в фандрейзингу
    rfm_recency             INTEGER,
    rfm_frequency           INTEGER,
    rfm_monetary            INTEGER,

    segment                 TEXT,            -- див. segment_definition
    segment_changed_at      TIMESTAMP,
    updated_at              TIMESTAMP NOT NULL
);

CREATE INDEX ix_metrics_segment ON donor_metrics(segment);

-- Історія зміни сегментів: дозволяє бачити переходи
-- (скільки донорів у цьому кварталі перейшли з active у lapsed).
CREATE TABLE donor_segment_history (
    history_id          TEXT PRIMARY KEY,
    donor_id            TEXT NOT NULL REFERENCES donor(donor_id),
    segment_from        TEXT,
    segment_to          TEXT NOT NULL,
    changed_at          TIMESTAMP NOT NULL,
    trigger_donation_id TEXT REFERENCES donation(donation_id)
);


-- =============================================================================
-- ШАР OPS — контроль за роботою автоматизації
-- =============================================================================

-- Черга ручного розгляду. Все, у чому алгоритм не впевнений,
-- потрапляє сюди, а не мерджиться "на око".
CREATE TABLE match_review_queue (
    review_id           TEXT PRIMARY KEY,
    raw_id              TEXT NOT NULL REFERENCES raw_donation(raw_id),
    candidates          TEXT NOT NULL,       -- JSON: [{donor_id, score, reason}, ...]
    top_score           REAL,
    reason              TEXT NOT NULL,       -- 'ambiguous_match' | 'incomplete_data' | 'amount_anomaly'
    status              TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','resolved','escalated')),
    created_at          TIMESTAMP NOT NULL,
    resolved_at         TIMESTAMP,
    resolved_by         TEXT,
    resolution          TEXT
);

CREATE INDEX ix_review_status ON match_review_queue(status, created_at);

-- Журнал мерджів. Робить операцію оборотною.
CREATE TABLE merge_log (
    merge_id            TEXT PRIMARY KEY,
    winner_donor_id     TEXT NOT NULL REFERENCES donor(donor_id),
    loser_donor_id      TEXT NOT NULL REFERENCES donor(donor_id),
    merged_at           TIMESTAMP NOT NULL,
    merged_by           TEXT NOT NULL,
    match_score         REAL,
    snapshot_before     TEXT,                -- JSON стану обох записів до мерджу
    is_reverted         INTEGER NOT NULL DEFAULT 0
);

-- Запуски автоматизації: без цього неможливо відповісти
-- "чому вчора не пішли листи подяки".
CREATE TABLE automation_run (
    run_id              TEXT PRIMARY KEY,
    job_name            TEXT NOT NULL,
    started_at          TIMESTAMP NOT NULL,
    finished_at         TIMESTAMP,
    status              TEXT NOT NULL CHECK (status IN ('running','success','partial','failed')),
    records_in          INTEGER DEFAULT 0,
    records_ok          INTEGER DEFAULT 0,
    records_review      INTEGER DEFAULT 0,
    records_failed      INTEGER DEFAULT 0,
    error_message       TEXT
);

-- Звірка: сума в CRM за період має дорівнювати сумі у виписці каналу.
-- Це головний запобіжник проти тихої втрати або дублювання даних.
CREATE TABLE ops_reconciliation (
    recon_id            TEXT PRIMARY KEY,
    batch_id            TEXT REFERENCES ingestion_batch(batch_id),
    source_id           TEXT NOT NULL REFERENCES source(source_id),
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    source_total        REAL NOT NULL,       -- за даними джерела
    crm_total           REAL NOT NULL,       -- за даними core.donation
    difference          REAL NOT NULL,
    source_count        INTEGER,
    crm_count           INTEGER,
    status              TEXT NOT NULL CHECK (status IN ('matched','discrepancy','investigating','resolved')),
    checked_at          TIMESTAMP NOT NULL,
    notes               TEXT
);

-- Проблеми якості даних як окрема сутність, щоб їх можна було
-- рахувати й показувати на дашборді, а не ловити в логах.
CREATE TABLE data_quality_issue (
    issue_id            TEXT PRIMARY KEY,
    entity_type         TEXT NOT NULL CHECK (entity_type IN ('donor','donation','raw_donation')),
    entity_id           TEXT NOT NULL,
    issue_type          TEXT NOT NULL,       -- 'missing_email' | 'invalid_amount' | 'unparseable_date' | ...
    severity            TEXT NOT NULL CHECK (severity IN ('low','medium','high')),
    detected_at         TIMESTAMP NOT NULL,
    resolved_at         TIMESTAMP,
    details             TEXT
);

CREATE INDEX ix_dq_open ON data_quality_issue(issue_type, resolved_at);


-- =============================================================================
-- ПРЕДСТАВЛЕННЯ ДЛЯ АНАЛІТИКИ
-- =============================================================================

-- Активні донори без урахування злитих дублікатів.
-- Кожен аналітичний запит має ходити сюди, а не в donor напряму.
CREATE VIEW v_donor_active AS
SELECT * FROM donor
WHERE merged_into_donor_id IS NULL
  AND donor_status <> 'merged';

-- Пожертви, придатні для фінансової аналітики:
-- без невдалих спроб, з поверненнями як від'ємними сумами.
CREATE VIEW v_donation_clean AS
SELECT d.*
FROM donation d
WHERE d.donation_status IN ('completed','refunded','chargeback');
