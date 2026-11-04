/* Narrow libproc observer. No arbitrary PID is accepted from IPC: callers pass
 * only retained, unreaped supervisor roots. Partial/denied/churned observations
 * return failure. SDK ABI and native containment remain target acceptance gates. */
#include <libproc.h>
#include <sys/proc_info.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#define MAX_PROCS 1024
#define MAX_FDS 8192

static int compare_pid(const void *a,const void *b){pid_t x=*(const pid_t*)a,y=*(const pid_t*)b;return (x>y)-(x<y);}
static int group(pid_t leader,pid_t out[MAX_PROCS],int *count){
  int n=proc_listpids(PROC_PGRP_ONLY,(uint32_t)leader,out,MAX_PROCS*sizeof(pid_t));
  if(n<=0 || n>=(int)(MAX_PROCS*sizeof(pid_t)) || n%(int)sizeof(pid_t))return 0;
  int used=0;for(int i=0;i<n/(int)sizeof(pid_t);i++)if(out[i]>0){if(getpgid(out[i])!=leader)return 0;out[used++]=out[i];}
  if(!used)return 0;qsort(out,used,sizeof(pid_t),compare_pid);
  for(int i=1;i<used;i++)if(out[i]==out[i-1])return 0;*count=used;return 1;
}
struct identity { uint64_t sec,usec; };
static int identity(pid_t pid,struct identity *out){struct proc_bsdinfo info;
  if(proc_pidinfo(pid,PROC_PIDTBSDINFO,0,&info,sizeof(info))!=(int)sizeof(info))return 0;
  out->sec=info.pbi_start_tvsec;out->usec=info.pbi_start_tvusec;return 1;
}
static int identities(pid_t *pids,int n,struct identity *out){for(int i=0;i<n;i++)if(!identity(pids[i],&out[i]))return 0;return 1;}
static int same_identity(struct identity a,struct identity b){return a.sec==b.sec && a.usec==b.usec;}
static int rss(pid_t pid,uint64_t *value){struct proc_taskinfo info;
  if(proc_pidinfo(pid,PROC_PIDTASKINFO,0,&info,sizeof(info))!=(int)sizeof(info))return 0;
  *value=info.pti_resident_size;return 1;
}
static int has_tuple(pid_t pid,uint16_t local,uint16_t remote,int *matches){
  struct proc_fdinfo *fds=calloc(MAX_FDS,sizeof(*fds));if(!fds)return 0;
  int n=proc_pidinfo(pid,PROC_PIDLISTFDS,0,fds,MAX_FDS*sizeof(*fds));
  if(n<0 || n>=(int)(MAX_FDS*sizeof(*fds)) || n%(int)sizeof(*fds)){free(fds);return 0;}
  for(int i=0;i<n/(int)sizeof(*fds);i++)if(fds[i].proc_fdtype==PROX_FDTYPE_SOCKET){
    struct socket_fdinfo fd;
    if(proc_pidfdinfo(pid,fds[i].proc_fd,PROC_PIDFDSOCKETINFO,&fd,sizeof(fd))!=(int)sizeof(fd)){free(fds);return 0;}
    if(fd.psi.soi_family!=AF_INET || fd.psi.soi_type!=SOCK_STREAM || fd.psi.soi_kind!=SOCKINFO_TCP)continue;
    struct tcp_sockinfo *tcp=&fd.psi.soi_proto.pri_tcp;struct in_sockinfo *ip=&tcp->tcpsi_ini;
    if(tcp->tcpsi_state==TSI_S_ESTABLISHED && ntohs((uint16_t)ip->insi_lport)==local && ntohs((uint16_t)ip->insi_fport)==remote
      && ip->insi_laddr.ina_46.i46a_addr4.s_addr==htonl(INADDR_LOOPBACK)
      && ip->insi_faddr.ina_46.i46a_addr4.s_addr==htonl(INADDR_LOOPBACK))(*matches)++;
  }
  free(fds);return 1;
}
static int has_listener(pid_t pid,uint16_t port,int *matches){
  struct proc_fdinfo *fds=calloc(MAX_FDS,sizeof(*fds));if(!fds)return 0;
  int n=proc_pidinfo(pid,PROC_PIDLISTFDS,0,fds,MAX_FDS*sizeof(*fds));
  if(n<0 || n>=(int)(MAX_FDS*sizeof(*fds)) || n%(int)sizeof(*fds)){free(fds);return 0;}
  for(int i=0;i<n/(int)sizeof(*fds);i++)if(fds[i].proc_fdtype==PROX_FDTYPE_SOCKET){
    struct socket_fdinfo fd;if(proc_pidfdinfo(pid,fds[i].proc_fd,PROC_PIDFDSOCKETINFO,&fd,sizeof(fd))!=(int)sizeof(fd)){free(fds);return 0;}
    if(fd.psi.soi_family!=AF_INET || fd.psi.soi_type!=SOCK_STREAM || fd.psi.soi_kind!=SOCKINFO_TCP)continue;
    struct tcp_sockinfo *tcp=&fd.psi.soi_proto.pri_tcp;struct in_sockinfo *ip=&tcp->tcpsi_ini;
    if(tcp->tcpsi_state==TSI_S_LISTEN && ntohs((uint16_t)ip->insi_lport)==port && ip->insi_laddr.ina_46.i46a_addr4.s_addr==htonl(INADDR_LOOPBACK))(*matches)++;
  }free(fds);return 1;
}
int tr_macos_listener(pid_t runtime,uint16_t port){
  pid_t before[MAX_PROCS],after[MAX_PROCS];struct identity start[MAX_PROCS],end[MAX_PROCS];int n=0,m=0,matches=0;
  if(!port || !group(runtime,before,&n) || !identities(before,n,start))return 0;
  for(int i=0;i<n;i++)if(!has_listener(before[i],port,&matches))return 0;
  if(!group(runtime,after,&m) || m!=n || memcmp(before,after,n*sizeof(pid_t)) || !identities(after,n,end))return 0;
  for(int i=0;i<n;i++)if(!same_identity(start[i],end[i]))return 0;return matches==1;
}
int tr_macos_peer(pid_t worker,pid_t runtime,uint16_t client,uint16_t server){
  pid_t before[MAX_PROCS],after[MAX_PROCS];struct identity start[MAX_PROCS],end[MAX_PROCS],ws,we;
  int n=0,m=0,clients=0,servers=0,clients2=0,servers2=0;
  if(!client || !server || getpgid(worker)!=worker || !identity(worker,&ws) || !group(runtime,before,&n)
    || !identities(before,n,start) || !has_tuple(worker,client,server,&clients))return 0;
  for(int i=0;i<n;i++)if(!has_tuple(before[i],server,client,&servers))return 0;
  if(!group(runtime,after,&m) || m!=n || memcmp(before,after,n*sizeof(pid_t)) || !identities(after,n,end)
    || !identity(worker,&we) || !same_identity(ws,we) || getpgid(worker)!=worker)return 0;
  for(int i=0;i<n;i++)if(!same_identity(start[i],end[i]) || !has_tuple(after[i],server,client,&servers2))return 0;
  if(!has_tuple(worker,client,server,&clients2))return 0;
  return clients==1 && servers==1 && clients2==1 && servers2==1;
}
int tr_macos_rss(pid_t worker,pid_t runtime,uint64_t *worker_rss,uint64_t *runtime_rss){
  pid_t before[MAX_PROCS],after[MAX_PROCS];struct identity start[MAX_PROCS],end[MAX_PROCS],ws,we;
  int n=0,m=0;uint64_t total=0,value=0;
  if(getpgid(worker)!=worker || !identity(worker,&ws) || !rss(worker,worker_rss))return 0;
  if(runtime){
    if(!group(runtime,before,&n) || !identities(before,n,start))return 0;
    for(int i=0;i<n;i++){if(!rss(before[i],&value) || UINT64_MAX-total<value)return 0;total+=value;}
    if(!group(runtime,after,&m) || m!=n || memcmp(before,after,n*sizeof(pid_t)) || !identities(after,n,end))return 0;
    for(int i=0;i<n;i++)if(!same_identity(start[i],end[i]))return 0;
  }
  if(getpgid(worker)!=worker || !identity(worker,&we) || !same_identity(ws,we))return 0;
  *runtime_rss=total;return 1;
}

/* Native browser harness observation. Two exact FD/socket-identity samples are
 * bound to three complete retained-group/start-identity samples. This proves a
 * bounded observation, never an enduring claim about future process activity. */
#define MAX_BROWSER_GROUP 128
#define MAX_BROWSER_FD_RECORDS 65536
struct browser_fd_record {
  int fd,type,family,socket_type,kind,tcp_state;
  uint64_t socket,pcb;
};
struct browser_fd_set { struct browser_fd_record *records; int count; };
static int compare_browser_fd(const void *a,const void *b){
  const struct browser_fd_record *x=a,*y=b;return (x->fd>y->fd)-(x->fd<y->fd);
}
static int browser_fds(pid_t pid,int require_pipe,struct browser_fd_set *out,int *budget){
  struct proc_fdinfo *fds=calloc(MAX_FDS,sizeof(*fds));if(!fds)return 0;
  int n=proc_pidinfo(pid,PROC_PIDLISTFDS,0,fds,MAX_FDS*sizeof(*fds));
  if(n<=0 || n>=(int)(MAX_FDS*sizeof(*fds)) || n%(int)sizeof(*fds)){free(fds);return 0;}
  int used=n/(int)sizeof(*fds);if(used>*budget){free(fds);return 0;}*budget-=used;
  out->records=calloc((size_t)used,sizeof(*out->records));if(!out->records){free(fds);return 0;}
  out->count=used;int pipe3=0,pipe4=0,ok=0;
  for(int i=0;i<used;i++){
    struct browser_fd_record *record=&out->records[i];
    record->fd=fds[i].proc_fd;record->type=fds[i].proc_fdtype;
    if(record->fd<0)goto done;
    if(require_pipe && (record->fd==3 || record->fd==4) && record->type!=PROX_FDTYPE_SOCKET)goto done;
    if(record->type!=PROX_FDTYPE_SOCKET)continue;
    struct socket_fdinfo fd;
    if(proc_pidfdinfo(pid,record->fd,PROC_PIDFDSOCKETINFO,&fd,sizeof(fd))!=(int)sizeof(fd))goto done;
    record->socket=fd.psi.soi_so;record->pcb=fd.psi.soi_pcb;
    record->family=fd.psi.soi_family;record->socket_type=fd.psi.soi_type;record->kind=fd.psi.soi_kind;
    if(!record->socket)goto done;
    if((record->family==AF_INET || record->family==AF_INET6) && record->socket_type==SOCK_STREAM && record->kind==SOCKINFO_TCP){
      record->tcp_state=fd.psi.soi_proto.pri_tcp.tcpsi_state;
      if(record->tcp_state==TSI_S_LISTEN)goto done;
    }
    if(require_pipe && (record->fd==3 || record->fd==4)){
      if(record->family!=AF_UNIX || record->socket_type!=SOCK_STREAM)goto done;
      if(record->fd==3)pipe3++;else pipe4++;
    }
  }
  if(require_pipe && (pipe3!=1 || pipe4!=1))goto done;
  qsort(out->records,(size_t)used,sizeof(*out->records),compare_browser_fd);
  for(int i=1;i<used;i++)if(out->records[i-1].fd==out->records[i].fd)goto done;
  ok=1;
done:
  free(fds);if(!ok){free(out->records);out->records=NULL;out->count=0;}return ok;
}
static int browser_same_group(pid_t browser,pid_t *before,struct identity *start,int n){
  pid_t after[MAX_PROCS];struct identity end[MAX_PROCS];int m=0;
  if(!group(browser,after,&m) || m!=n || memcmp(before,after,(size_t)n*sizeof(pid_t)) || !identities(after,n,end))return 0;
  for(int i=0;i<n;i++)if(!same_identity(start[i],end[i]))return 0;
  return getpgid(browser)==browser;
}
int tr_macos_browser_pipe(pid_t browser){
  pid_t before[MAX_PROCS];struct identity start[MAX_PROCS];int n=0,ok=0;
  struct browser_fd_set first[MAX_BROWSER_GROUP]={0},second[MAX_BROWSER_GROUP]={0};
  int budget=MAX_BROWSER_FD_RECORDS;
  if(browser<=0 || getpgid(browser)!=browser || !group(browser,before,&n) || n>MAX_BROWSER_GROUP || !identities(before,n,start))return 0;
  int leader_present=0;for(int i=0;i<n;i++)if(before[i]==browser)leader_present++;
  if(leader_present!=1)return 0;
  for(int i=0;i<n;i++)if(!browser_fds(before[i],before[i]==browser,&first[i],&budget))goto done;
  if(!browser_same_group(browser,before,start,n))goto done;
  budget=MAX_BROWSER_FD_RECORDS;
  for(int i=0;i<n;i++){
    if(!browser_fds(before[i],before[i]==browser,&second[i],&budget)
       || first[i].count!=second[i].count
       || memcmp(first[i].records,second[i].records,(size_t)first[i].count*sizeof(*first[i].records)))goto done;
  }
  /* Bind the completed final FD scans back to the originally retained PIDs. */
  if(!browser_same_group(browser,before,start,n))goto done;
  ok=1;
done:
  for(int i=0;i<MAX_BROWSER_GROUP;i++){free(first[i].records);free(second[i].records);}return ok;
}
