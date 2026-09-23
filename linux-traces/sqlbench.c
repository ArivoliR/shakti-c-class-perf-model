/* Android-representative: SQLite insert + indexed query workload, in-memory DB.
   Exercises B-tree pointer chasing, malloc traffic, memcpy, string compare. */
#include <stdio.h>
#include "sqlite3.h"
int main(void){
  sqlite3 *db; char *err=0; sqlite3_stmt *st;
  if(sqlite3_open(":memory:",&db)) return 1;
  sqlite3_exec(db,"PRAGMA journal_mode=OFF;PRAGMA synchronous=OFF;",0,0,&err);
  sqlite3_exec(db,"CREATE TABLE t(k INTEGER PRIMARY KEY, v TEXT, n INTEGER);",0,0,&err);
  sqlite3_exec(db,"BEGIN;",0,0,&err);
  sqlite3_prepare_v2(db,"INSERT INTO t VALUES(?,?,?)",-1,&st,0);
  char buf[64];
  for(int i=0;i<4000;i++){
    snprintf(buf,sizeof buf,"row-%d-payload-string",i);
    sqlite3_bind_int(st,1,i); sqlite3_bind_text(st,2,buf,-1,SQLITE_TRANSIENT);
    sqlite3_bind_int(st,3,(i*2654435761u)&0xffff);
    sqlite3_step(st); sqlite3_reset(st);
  }
  sqlite3_finalize(st);
  sqlite3_exec(db,"COMMIT;",0,0,&err);
  sqlite3_exec(db,"CREATE INDEX ix ON t(n);",0,0,&err);
  long acc=0;
  sqlite3_prepare_v2(db,"SELECT count(*),sum(k) FROM t WHERE n > ? AND n < ?",-1,&st,0);
  for(int q=0;q<200;q++){
    sqlite3_bind_int(st,1,(q*97)&0xffff); sqlite3_bind_int(st,2,((q*97)&0xffff)+3000);
    while(sqlite3_step(st)==SQLITE_ROW) acc += sqlite3_column_int64(st,1);
    sqlite3_reset(st);
  }
  sqlite3_finalize(st); sqlite3_close(db);
  printf("acc=%ld\n",acc); return 0;
}
