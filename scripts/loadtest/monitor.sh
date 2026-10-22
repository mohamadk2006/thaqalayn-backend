#!/bin/sh
# Records the server's state every 10 s while a load test runs (run as root on the server):
#   sh monitor.sh > /root/loadtest-$(date +%F-%H%M).csv     # Ctrl-C to stop
# Columns: time, 1-minute load, free RAM (MB), CPU % of the API / Postgres / Qdrant
# containers, Postgres connections (all / running a query), the longest running query (s),
# and the time of one real search from the server itself (s).
API=$(docker ps --format '{{.Names}}' | grep '^jxrrpybwb0mijjrq8qd1j7fs' | head -1)
PG=huf6d33j3nrd4wr9mx5wn4bv
PROBE='https://api.thaqalaynlibrary.com/api/search?q=%D8%AE%D8%B2%D8%A7%D8%A6%D9%86&limit=20'
echo "time,load1,free_mb,api_cpu,pg_cpu,qdrant_cpu,pg_conns,pg_active,pg_longest_s,probe_s"
while true; do
  load=$(cut -d' ' -f1 /proc/loadavg)
  free_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
  cpus=$(docker stats --no-stream --format '{{.Name}} {{.CPUPerc}}' "$API" "$PG" thaqalayn-qdrant \
         | awk '{gsub("%","",$2); printf "%s,", $2}')
  pg=$(docker exec "$PG" psql -U thaqalayn -d thaqalayn -Atc \
       "select count(*), count(*) filter (where state='active'),
               coalesce(round(extract(epoch from max(now()-query_start) filter (where state='active'))::numeric,1),0)
        from pg_stat_activity where datname='thaqalayn' and pid <> pg_backend_pid()" | tr '|' ',')
  probe=$(curl -s -o /dev/null -m 60 -w '%{time_total}' "$PROBE")
  echo "$(date +%T),$load,$free_mb,${cpus}${pg},$probe"
  sleep 10
done
