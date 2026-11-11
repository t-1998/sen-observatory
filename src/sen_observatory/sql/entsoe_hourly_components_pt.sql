-- ENTSO-E Portugal as hourly MW per REN component, using the mapping in component_map
-- (pt_component_map.sql, which must be loaded on the connection first).
--
-- Only complete hours are returned, and a component made of several ENTSO-E variables needs all
-- of them in that hour (same rule as compare_ren_entsoe_pt.sql). Series at 15 min are averaged to
-- the hour. local_hour is the start of the hour in Europe/Lisbon time.
--
-- Parameters: ? = first local day, ? = last local day (both inclusive)
WITH en_v AS (
  SELECT date_trunc('hour', e.ts_utc) AS hour_utc, m.component, e.variable, m.sign, avg(e.value) AS mw
  FROM entsoe_timeseries e
  JOIN component_map m ON m.entsoe_variable = e.variable AND m.entsoe_country = e.country
  WHERE m.status = 'compared'
  GROUP BY 1, 2, 3, 4
  HAVING count(*) = 60 / min(e.resolution_min)
),
expected AS (
  SELECT component, count(*) AS n_vars FROM component_map WHERE status = 'compared' GROUP BY 1
)
SELECT timezone('Europe/Lisbon', v.hour_utc) AS local_hour, v.component, sum(v.sign * v.mw) AS mw
FROM en_v v JOIN expected x USING (component)
WHERE CAST(timezone('Europe/Lisbon', v.hour_utc) AS DATE) BETWEEN ? AND ?
GROUP BY v.hour_utc, v.component, x.n_vars
HAVING count(*) = x.n_vars
ORDER BY 1, 2;
