import sys, yaml, psycopg
cfg = yaml.safe_load(open("/opt/stackstorm/configs/cdr.yaml"))
with psycopg.connect(cfg["supabase_ops_url"], autocommit=True, prepare_threshold=None) as c:
    cur = c.execute(sys.argv[1])
    for r in cur.fetchall():
        print(*r, sep=" | ")
