/*
 * Insert benchmark: N client threads insert rows into table t, B rows per
 * transaction (one multi-row INSERT in autocommit; B=1 is a plain single-row
 * insert), using one of several key generators. Client-side keys are bound as
 * params; the nextseq workloads leave the id to the server's nextsequence.
 *
 * With -w update or -w read, each client instead updates or selects one row
 * at a time by a random key from those that hv4 inserts numbered 1..K (-K).
 * hv4 keys are v4-shaped but derived from a counter, so they can be found
 * again; top keys sort above every hv4 key, so inserting them appends.
 *
 * Writes:
 *   <prefix>.ts.csv       one row per sample interval (client throughput,
 *                         commit latency, server metric deltas)
 *   <prefix>.summary.csv  one row for the whole run
 *   <prefix>.hist.csv     commit-latency histogram for the whole run
 */
#include <getopt.h>
#include <inttypes.h>
#include <math.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <cdb2api.h>

enum keytype { INT_SEQ, INT_RAND, SEQ16, UUID_V7, UUID_V4, NEXTSEQ, NEXTSEQ_V7, NEXTSEQ_V4, HASHED_V4, TOP };
static const char *keynames[] = {"int_seq", "int_rand", "seq16", "v7",         "v4",
                                 "nextseq", "nextseq_v7", "nextseq_v4", "hv4", "top"};
enum op { OP_INSERT, OP_UPDATE, OP_READ };
static const char *opnames[] = {"insert", "update", "read"};
#define NKEYTYPES (int)(sizeof(keynames) / sizeof(keynames[0]))

#define PADLEN 100
#define NBUCKETS 256 /* latency histogram: 8 buckets per power of two of us */

static char *dbname, *cfgfile, *prefix, *label = "";
static enum keytype keytype;
static int nthreads = 1, batch = 100, interval_ms = 1000;
static enum op op = OP_INSERT;
static int64_t total_rows, nkeys, seq_start = 1, duration_s;
static int stopping; /* atomic; set when the -D duration is up */

static int64_t batches_claimed; /* atomic */
static int64_t rows_committed;  /* atomic */
static int64_t errors;          /* atomic */
static uint64_t hist[NBUCKETS]; /* atomic */
static int64_t seq_counter;     /* atomic; next int_seq/seq16/hv4/top key */
static int64_t not_found;       /* atomic; reads that returned no row */

static uint64_t now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000000ULL + ts.tv_nsec / 1000;
}

static uint64_t splitmix64(uint64_t *s)
{
    uint64_t z = (*s += 0x9e3779b97f4a7c15ULL);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

static int bucket_of(uint64_t us)
{
    if (us < 1)
        us = 1;
    int lg = 63 - __builtin_clzll(us);
    int sub = lg >= 3 ? (int)((us >> (lg - 3)) & 7) : (int)((us << (3 - lg)) & 7);
    int b = lg * 8 + sub;
    return b < NBUCKETS ? b : NBUCKETS - 1;
}

static double bucket_us(int b)
{
    int lg = b / 8, sub = b % 8;
    return ldexp(1.0 + (sub + 0.5) / 8.0, lg);
}

static double pctl(const uint64_t *h, double p)
{
    uint64_t n = 0;
    for (int i = 0; i < NBUCKETS; i++)
        n += h[i];
    if (n == 0)
        return 0;
    uint64_t want = (uint64_t)ceil(p * n), seen = 0;
    for (int i = 0; i < NBUCKETS; i++) {
        seen += h[i];
        if (seen >= want)
            return bucket_us(i);
    }
    return bucket_us(NBUCKETS - 1);
}

static void put_be64(unsigned char *p, uint64_t v)
{
    for (int i = 7; i >= 0; i--, v >>= 8)
        p[i] = v & 0xff;
}

static void fill_random(unsigned char *p, int len, uint64_t *rs)
{
    for (int i = 0; i < len; i += 8) {
        uint64_t r = splitmix64(rs);
        memcpy(p + i, &r, len - i < 8 ? len - i : 8);
    }
}

/* RFC 9562 layouts. v7: 48-bit unix ms, then random (no intra-ms counter). */
static void make_uuid(unsigned char *u, int version, uint64_t *rs)
{
    fill_random(u, 16, rs);
    if (version == 7) {
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        uint64_t ms = ts.tv_sec * 1000ULL + ts.tv_nsec / 1000000;
        unsigned char be[8];
        put_be64(be, ms);
        memcpy(u, be + 2, 6);
    }
    u[6] = (u[6] & 0x0f) | (version << 4);
    u[8] = (u[8] & 0x3f) | 0x80;
}

static void make_hashed_v4(unsigned char *u, int64_t n)
{
    uint64_t s = (uint64_t)n;
    uint64_t a = splitmix64(&s), b = splitmix64(&s);
    memcpy(u, &a, 8);
    memcpy(u + 8, &b, 8);
    u[6] = (u[6] & 0x0f) | 0x40;
    u[8] = (u[8] & 0x3f) | 0x80;
}

struct row {
    int64_t ik;
    unsigned char bk[16];
    int64_t v;
    unsigned char pad[PADLEN];
};

static void *writer(void *arg)
{
    int id = (int)(intptr_t)arg;
    uint64_t rs = now_us() * 1000003ULL + id * 7919ULL;
    cdb2_hndl_tp *h = NULL;
    int rc = cdb2_open(&h, dbname, "local", 0);
    if (rc) {
        fprintf(stderr, "thread %d: cdb2_open rc %d %s\n", id, rc, cdb2_errstr(h));
        exit(1);
    }

    int isint = keytype == INT_SEQ || keytype == INT_RAND;
    struct row *rows = calloc(batch, sizeof(*rows));
    size_t sqllen = 64 + (size_t)batch * 48;
    char *sql = malloc(sqllen);
    /* nextseq tables: id is filled in by the server; the uuid goes in column u. */
    const char *cols = keytype == NEXTSEQ ? "v, pad" : keytype == NEXTSEQ_V7 || keytype == NEXTSEQ_V4 ? "u, v, pad" : "k, v, pad";
    int off = 0;
    if (op == OP_INSERT)
        off = snprintf(sql, sqllen, "insert into t(%s) values ", cols);
    /* cdb2_bind_param keeps the name pointer, so names must outlive the run. */
    char (*names)[3][16] = calloc(batch, sizeof(*names));
    for (int i = 0; op == OP_INSERT && i < batch; i++) {
        if (keytype == NEXTSEQ)
            off += snprintf(sql + off, sqllen - off, "%s(@v%d,@p%d)", i ? "," : "", i, i);
        else
            off += snprintf(sql + off, sqllen - off, "%s(@k%d,@v%d,@p%d)", i ? "," : "", i, i, i);
        snprintf(names[i][0], 16, "k%d", i);
        snprintf(names[i][1], 16, "v%d", i);
        snprintf(names[i][2], 16, "p%d", i);
        if (keytype == NEXTSEQ)
            ;
        else if (isint)
            cdb2_bind_param(h, names[i][0], CDB2_INTEGER, &rows[i].ik, sizeof(int64_t));
        else
            cdb2_bind_param(h, names[i][0], CDB2_BLOB, rows[i].bk, 16);
        cdb2_bind_param(h, names[i][1], CDB2_INTEGER, &rows[i].v, sizeof(int64_t));
        cdb2_bind_param(h, names[i][2], CDB2_BLOB, rows[i].pad, PADLEN);
    }
    if (op != OP_INSERT) {
        snprintf(sql, sqllen, op == OP_UPDATE ? "update t set v = @v0 where k = @k0" : "select v, pad from t where k = @k0");
        snprintf(names[0][0], 16, "k0");
        snprintf(names[0][1], 16, "v0");
        cdb2_bind_param(h, names[0][0], CDB2_BLOB, rows[0].bk, 16);
        if (op == OP_UPDATE)
            cdb2_bind_param(h, names[0][1], CDB2_INTEGER, &rows[0].v, sizeof(int64_t));
    }

    int64_t nbatches = (total_rows + batch - 1) / batch;
    while (!__atomic_load_n(&stopping, __ATOMIC_RELAXED) &&
           __atomic_fetch_add(&batches_claimed, 1, __ATOMIC_RELAXED) < nbatches) {
        int64_t seq = 0;
        if (keytype == INT_SEQ || keytype == SEQ16 || keytype == HASHED_V4 || keytype == TOP)
            seq = __atomic_fetch_add(&seq_counter, batch, __ATOMIC_RELAXED);
        for (int i = 0; i < batch; i++) {
            struct row *r = &rows[i];
            switch (keytype) {
            case INT_SEQ: r->ik = seq + i; break;
            case INT_RAND: r->ik = (int64_t)splitmix64(&rs); break;
            case SEQ16: memset(r->bk, 0, 8); put_be64(r->bk + 8, seq + i); break;
            case UUID_V7: make_uuid(r->bk, 7, &rs); break;
            case UUID_V4: make_uuid(r->bk, 4, &rs); break;
            case NEXTSEQ: break;
            case NEXTSEQ_V7: make_uuid(r->bk, 7, &rs); break;
            case NEXTSEQ_V4: make_uuid(r->bk, 4, &rs); break;
            case HASHED_V4:
                make_hashed_v4(r->bk, op == OP_INSERT ? seq + i : 1 + (int64_t)(splitmix64(&rs) % nkeys));
                break;
            case TOP: memset(r->bk, 0xff, 8); put_be64(r->bk + 8, seq + i); break;
            }
            r->v = (int64_t)splitmix64(&rs);
            fill_random(r->pad, PADLEN, &rs);
        }
        for (int attempt = 0;; attempt++) {
            if (attempt == 100) {
                fprintf(stderr, "thread %d: giving up after %d failed attempts\n", id, attempt);
                exit(1);
            }
            uint64_t t0 = now_us();
            rc = cdb2_run_statement(h, sql);
            if (rc == CDB2_OK) {
                int nrec = 0;
                while ((rc = cdb2_next_record(h)) == CDB2_OK)
                    nrec++;
                if (op == OP_READ && nrec == 0)
                    __atomic_fetch_add(&not_found, 1, __ATOMIC_RELAXED);
                if (rc == CDB2_OK_DONE)
                    rc = 0;
            }
            uint64_t t1 = now_us();
            if (rc == 0) {
                __atomic_fetch_add(&hist[bucket_of(t1 - t0)], 1, __ATOMIC_RELAXED);
                __atomic_fetch_add(&rows_committed, batch, __ATOMIC_RELAXED);
                break;
            }
            if (__atomic_fetch_add(&errors, 1, __ATOMIC_RELAXED) < 20)
                fprintf(stderr, "thread %d: rc %d %s\n", id, rc, cdb2_errstr(h));
            /* A colliding random int key fails the whole batch; draw new keys. */
            if (keytype == INT_RAND)
                for (int i = 0; i < batch; i++)
                    rows[i].ik = (int64_t)splitmix64(&rs);
        }
    }
    cdb2_close(h);
    free(names);
    free(rows);
    free(sql);
    return NULL;
}

static const char *metric_names[] = {
    "lockwaits",   "lockwait_time", "deadlocks",  "locks_aborted",     "lockrequests",
    "retries",     "cache_hits",    "cache_misses", "leaf_cache_hits", "leaf_cache_misses",
    "page_reads",  "page_writes",   "fsyncs",     "commits"};
#define NMETRICS (int)(sizeof(metric_names) / sizeof(metric_names[0]))

static void read_metrics(cdb2_hndl_tp *h, int64_t *out)
{
    static char sql[1024];
    if (!sql[0]) {
        int off = snprintf(sql, sizeof(sql), "select name, value from comdb2_metrics where name in (");
        for (int i = 0; i < NMETRICS; i++)
            off += snprintf(sql + off, sizeof(sql) - off, "%s'%s'", i ? "," : "", metric_names[i]);
        snprintf(sql + off, sizeof(sql) - off, ")");
    }
    memset(out, 0, sizeof(int64_t) * NMETRICS);
    if (cdb2_run_statement(h, sql) != CDB2_OK) {
        fprintf(stderr, "metrics query failed: %s\n", cdb2_errstr(h));
        return;
    }
    while (cdb2_next_record(h) == CDB2_OK) {
        const char *name = cdb2_column_value(h, 0);
        void *val = cdb2_column_value(h, 1);
        if (!name || !val)
            continue;
        for (int i = 0; i < NMETRICS; i++) {
            if (strcmp(name, metric_names[i]) == 0) {
                if (cdb2_column_type(h, 1) == CDB2_INTEGER)
                    out[i] = *(int64_t *)val;
                else if (cdb2_column_type(h, 1) == CDB2_REAL)
                    out[i] = (int64_t) * (double *)val;
            }
        }
    }
}

static void usage(const char *argv0)
{
    fprintf(stderr,
            "usage: %s -d db -c cfg -k {int_seq,int_rand,seq16,v7,v4,hv4,top} {-n rows | -D seconds} -o prefix\n"
            "          [-T threads] [-b batch] [-i interval_ms] [-L label]\n"
            "          [-w insert|update|read] [-K hv4 keys to update/read] [-s first hv4/top counter]\n",
            argv0);
    exit(2);
}

int main(int argc, char **argv)
{
    int c, havekey = 0;
    while ((c = getopt(argc, argv, "d:c:k:n:o:T:b:i:L:w:K:s:D:")) != -1) {
        switch (c) {
        case 'd': dbname = optarg; break;
        case 'c': cfgfile = optarg; break;
        case 'k':
            for (int i = 0; i < NKEYTYPES; i++)
                if (strcmp(optarg, keynames[i]) == 0)
                    keytype = i, havekey = 1;
            break;
        case 'n': total_rows = atoll(optarg); break;
        case 'o': prefix = optarg; break;
        case 'T': nthreads = atoi(optarg); break;
        case 'b': batch = atoi(optarg); break;
        case 'i': interval_ms = atoi(optarg); break;
        case 'L': label = optarg; break;
        case 'w':
            for (int i = 0; i < 3; i++)
                if (strcmp(optarg, opnames[i]) == 0)
                    op = i;
            break;
        case 'K': nkeys = atoll(optarg); break;
        case 's': seq_start = atoll(optarg); break;
        case 'D': duration_s = atoll(optarg); break;
        default: usage(argv[0]);
        }
    }
    if (duration_s > 0 && total_rows <= 0)
        total_rows = INT64_MAX / 2;
    if (!dbname || !havekey || total_rows <= 0 || !prefix || nthreads < 1 || batch < 1)
        usage(argv[0]);
    if (op != OP_INSERT && (keytype != HASHED_V4 || nkeys < 1 || batch != 1))
        usage(argv[0]);
    if (cfgfile)
        cdb2_set_comdb2db_config(cfgfile);
    /* cdb2_close hands sockets to the host's sockpool without MSG_NOSIGNAL; if
     * sockpool restarted during the run, that write would kill the process. */
    signal(SIGPIPE, SIG_IGN);
    seq_counter = seq_start;

    cdb2_hndl_tp *mh = NULL;
    if (cdb2_open(&mh, dbname, "local", 0)) {
        fprintf(stderr, "cdb2_open: %s\n", cdb2_errstr(mh));
        return 1;
    }

    char path[4096];
    snprintf(path, sizeof(path), "%s.ts.csv", prefix);
    FILE *ts = fopen(path, "w");
    if (!ts) {
        perror(path);
        return 1;
    }
    fprintf(ts, "t_s,rows,rows_per_s,lat_p50_us,lat_p99_us");
    for (int i = 0; i < NMETRICS; i++)
        fprintf(ts, ",%s", metric_names[i]);
    fprintf(ts, "\n");

    int64_t m0[NMETRICS], mprev[NMETRICS], mcur[NMETRICS];
    read_metrics(mh, m0);
    memcpy(mprev, m0, sizeof(m0));

    pthread_t *tids = calloc(nthreads, sizeof(pthread_t));
    uint64_t start = now_us();
    for (int i = 0; i < nthreads; i++)
        pthread_create(&tids[i], NULL, writer, (void *)(intptr_t)i);

    uint64_t hprev[NBUCKETS] = {0}, hcur[NBUCKETS], hdiff[NBUCKETS];
    int64_t rows_prev = 0;
    uint64_t tprev = start;
    for (;;) {
        int64_t target = total_rows;
        uint64_t next = tprev + interval_ms * 1000ULL;
        while (now_us() < next && __atomic_load_n(&rows_committed, __ATOMIC_RELAXED) < target)
            usleep(10000);
        uint64_t t = now_us();
        int finished = __atomic_load_n(&rows_committed, __ATOMIC_RELAXED) >= target ||
                       (duration_s > 0 && t - start >= duration_s * 1000000ULL);
        int64_t rows = __atomic_load_n(&rows_committed, __ATOMIC_RELAXED);
        for (int i = 0; i < NBUCKETS; i++) {
            hcur[i] = __atomic_load_n(&hist[i], __ATOMIC_RELAXED);
            hdiff[i] = hcur[i] - hprev[i];
        }
        read_metrics(mh, mcur);
        double dt = (t - tprev) / 1e6;
        fprintf(ts, "%.3f,%" PRId64 ",%.1f,%.0f,%.0f", (t - start) / 1e6, rows, (rows - rows_prev) / dt,
                pctl(hdiff, 0.50), pctl(hdiff, 0.99));
        for (int i = 0; i < NMETRICS; i++)
            fprintf(ts, ",%" PRId64, mcur[i] - mprev[i]);
        fprintf(ts, "\n");
        fflush(ts);
        memcpy(hprev, hcur, sizeof(hcur));
        memcpy(mprev, mcur, sizeof(mcur));
        rows_prev = rows;
        tprev = t;
        if (finished)
            break;
    }
    __atomic_store_n(&stopping, 1, __ATOMIC_RELAXED);
    for (int i = 0; i < nthreads; i++)
        pthread_join(tids[i], NULL);
    double elapsed = (now_us() - start) / 1e6;
    fclose(ts);

    snprintf(path, sizeof(path), "%s.summary.csv", prefix);
    FILE *sm = fopen(path, "w");
    if (!sm) {
        perror(path);
        return 1;
    }
    fprintf(sm, "label,key,threads,batch,rows,elapsed_s,rows_per_s,lat_p50_us,lat_p99_us,lat_p999_us,errors");
    for (int i = 0; i < NMETRICS; i++)
        fprintf(sm, ",%s", metric_names[i]);
    fprintf(sm, "\n%s,%s,%d,%d,%" PRId64 ",%.3f,%.1f,%.0f,%.0f,%.0f,%" PRId64, label, keynames[keytype],
            nthreads, batch, rows_committed, elapsed, rows_committed / elapsed, pctl(hcur, 0.50),
            pctl(hcur, 0.99), pctl(hcur, 0.999), errors);
    for (int i = 0; i < NMETRICS; i++)
        fprintf(sm, ",%" PRId64, mcur[i] - m0[i]);
    fprintf(sm, "\n");
    fclose(sm);

    snprintf(path, sizeof(path), "%s.hist.csv", prefix);
    FILE *hf = fopen(path, "w");
    if (!hf) {
        perror(path);
        return 1;
    }
    fprintf(hf, "latency_us,count\n");
    for (int i = 0; i < NBUCKETS; i++)
        if (hcur[i])
            fprintf(hf, "%.0f,%" PRIu64 "\n", bucket_us(i), hcur[i]);
    fclose(hf);
    cdb2_close(mh);
    printf("%s %s %s T=%d rows=%" PRId64 " %.1fs %.0f rows/s p50=%.0fus p99=%.0fus errors=%" PRId64
           " not_found=%" PRId64 "\n",
           label, opnames[op], keynames[keytype], nthreads, rows_committed, elapsed, rows_committed / elapsed,
           pctl(hcur, 0.50), pctl(hcur, 0.99), errors, not_found);
    return 0;
}
