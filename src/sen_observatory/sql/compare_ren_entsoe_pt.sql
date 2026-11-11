-- REN Data Hub vs ENTSO-E for Portugal: energy (MWh) per local day, period and component.
--
-- Needs the temp table component_map (pt_component_map.sql), ren_generation and entsoe_timeseries.
--
-- Method
--   * Common resolution: one UTC hour. REN (15 min) is averaged over the 4 readings of the hour;
--     ENTSO-E series at 15 min are averaged over the hour too. Only complete hours are used, and a
--     component made of several ENTSO-E variables needs all of them in that hour.
--   * Energy: mean power of the hour (MW) x 1 h = MWh. Assumes ENTSO-E values are the average
--     power over their interval, as REN's are.
--   * Day: local day in Europe/Lisbon (23, 24 or 25 hours on daylight-saving days).
--   * diff = REN - ENTSO-E; diff_pct = diff / ENTSO-E.
--   * 2025-04-28 is also split by local hour: before 11:00, the 11:00-12:00 transition hour (it contains
--     11:30, ENTSO-E being hourly) and after 12:00.
WITH
ren_q AS (                                  -- REN, 15 min, long format
  SELECT ts_utc, component, value
  FROM (
    UNPIVOT (
      SELECT ts_utc, hydro, wind, solar, biomass, wave, natural_gas, coal, other_thermal,
             battery_discharge, pumping, consumption, imports - exports AS net_interconnection
      FROM ren_generation
    )
    ON hydro, wind, solar, biomass, wave, natural_gas, coal, other_thermal,
       battery_discharge, pumping, consumption, net_interconnection
    INTO NAME component VALUE value
  )
),
ren_h AS (
  SELECT date_trunc('hour', ts_utc) AS hour_utc, component, avg(value) AS mw
  FROM ren_q
  GROUP BY 1, 2
  HAVING count(*) = 4
),
gen_ren AS (SELECT DISTINCT component FROM component_map WHERE is_generation AND status IN ('compared', 'no_entsoe_category')),
gen_en  AS (SELECT DISTINCT component FROM component_map WHERE is_generation AND status = 'compared'),
ren_total AS (
  SELECT hour_utc, 'total_generation' AS component, sum(mw) AS mw
  FROM ren_h JOIN gen_ren USING (component)
  GROUP BY 1
  HAVING count(*) = (SELECT count(*) FROM gen_ren)
),
en_v AS (                                   -- ENTSO-E, one value per variable and complete hour
  SELECT date_trunc('hour', e.ts_utc) AS hour_utc, m.component, e.variable, m.sign, avg(e.value) AS mw
  FROM entsoe_timeseries e
  JOIN component_map m ON m.entsoe_variable = e.variable AND m.entsoe_country = e.country
  WHERE m.status = 'compared'
  GROUP BY 1, 2, 3, 4
  HAVING count(*) = 60 / min(e.resolution_min)
),
expected AS (
  SELECT component, count(*) AS n_vars FROM component_map WHERE status = 'compared' GROUP BY 1
),
en_h AS (                                   -- ENTSO-E component = sum of its variables, all present
  SELECT v.hour_utc, v.component, sum(v.sign * v.mw) AS mw
  FROM en_v v JOIN expected x USING (component)
  GROUP BY v.hour_utc, v.component, x.n_vars
  HAVING count(*) = x.n_vars
),
en_total AS (
  SELECT hour_utc, 'total_generation' AS component, sum(mw) AS mw
  FROM en_h JOIN gen_en USING (component)
  GROUP BY 1
  HAVING count(*) = (SELECT count(*) FROM gen_en)
),
comps AS (                                  -- components to report: status from component_map, fixed order
  SELECT o.component, coalesce(m.status, 'compared') AS status, o.ord
  FROM (VALUES
      ('hydro', 1), ('wind', 2), ('solar', 3), ('biomass', 4), ('natural_gas', 5), ('coal', 6),
      ('other_thermal', 7), ('wave', 8), ('battery_discharge', 9), ('total_generation', 10),
      ('pumping', 11), ('consumption', 12), ('net_interconnection', 13)
  ) AS o(component, ord)
  LEFT JOIN (SELECT DISTINCT component, status FROM component_map WHERE component IS NOT NULL) m
         USING (component)
),
j AS (
  SELECT r.hour_utc, r.component, c.status, c.ord, r.mw AS ren_mw,
         CASE WHEN c.status = 'no_entsoe_category' THEN 0 ELSE e.mw END AS en_mw
  FROM (SELECT * FROM ren_h UNION ALL SELECT * FROM ren_total) r
  JOIN comps c ON c.component = r.component
  LEFT JOIN (SELECT * FROM en_h UNION ALL SELECT * FROM en_total) e
         ON e.hour_utc = r.hour_utc AND e.component = r.component
),
h AS (
  SELECT *, timezone('Europe/Lisbon', hour_utc) AS local_ts FROM j WHERE en_mw IS NOT NULL
),
p AS (
  SELECT CAST(local_ts AS DATE) AS local_day, 0 AS period_ord, 'full_day' AS period,
         component, status, ord, ren_mw, en_mw
  FROM h
  UNION ALL
  SELECT CAST(local_ts AS DATE),
         CASE WHEN hour(local_ts) < 11 THEN 1 WHEN hour(local_ts) = 11 THEN 2 ELSE 3 END,
         CASE WHEN hour(local_ts) < 11 THEN 'before_11:00'
              WHEN hour(local_ts) = 11 THEN 'transition_11:00-12:00'
              ELSE 'after_12:00' END,
         component, status, ord, ren_mw, en_mw
  FROM h
  WHERE CAST(local_ts AS DATE) = DATE '2025-04-28'
)
SELECT local_day,
       period,
       component,
       status,
       count(*)                                     AS hours,
       round(sum(ren_mw), 1)                        AS ren_mwh,
       round(sum(en_mw), 1)                         AS entsoe_mwh,
       round(sum(ren_mw) - sum(en_mw), 1)           AS diff_mwh,
       round(100 * (sum(ren_mw) - sum(en_mw)) / nullif(sum(en_mw), 0), 2) AS diff_pct
FROM p
GROUP BY local_day, period_ord, period, component, status, ord
ORDER BY local_day, period_ord, ord;
