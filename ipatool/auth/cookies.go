package main

import (
    "bufio"
    "fmt"
    "net/http"
    "net/http/cookiejar"
    "net/url"
    "os"
    "path/filepath"
    "sort"
    "strconv"
    "strings"
    "sync"
    "time"
)

type savedCookie struct { cookie http.Cookie; hostOnly bool }
var cookieNow=time.Now
type loginCookies struct {
    jar *cookiejar.Jar
    mu sync.Mutex
    values map[string]savedCookie
    staged string
}
func cookieID(domain,path,name string) string { return domain+"\t"+path+"\t"+name }

func loadCookies(config string,secondStep bool) (*loginCookies,error) {
    jar,err:=cookiejar.New(nil); if err!=nil { return nil,err }
    result:=&loginCookies{jar:jar,values:map[string]savedCookie{},staged:filepath.Join(config,"auth-cookies")}
    path:=filepath.Join(config,"cookies")
    if secondStep {
        if info,e:=os.Stat(result.staged); e==nil {
            age:=cookieNow().Sub(info.ModTime())
            if age>=0 && age<10*time.Minute { path=result.staged }
        }
    }
    file,err:=os.Open(path)
    if os.IsNotExist(err) { return result,nil }
    if err!=nil { return nil,err }
    defer file.Close()
    info,err:=file.Stat(); if err!=nil || info.Size()>1<<20 { return nil,fmt.Errorf("cookie file too large") }
    scanner:=bufio.NewScanner(file)
    for scanner.Scan() {
        line:=scanner.Text(); httpOnly:=strings.HasPrefix(line,"#HttpOnly_")
        if httpOnly { line=strings.TrimPrefix(line,"#HttpOnly_") } else if strings.HasPrefix(line,"#") { continue }
        fields:=strings.SplitN(line,"\t",7); if len(fields)!=7 { continue }
        expiry,e:=strconv.ParseInt(fields[4],10,64); if e!=nil || expiry!=0 && expiry<=cookieNow().Unix() { continue }
        domain:=strings.TrimPrefix(fields[0],".")
        if domain!="apple.com" && !strings.HasSuffix(domain,".apple.com") { continue }
        c:=http.Cookie{Name:fields[5],Value:fields[6],Path:fields[2],Secure:fields[3]=="TRUE",HttpOnly:httpOnly}
        hostOnly:=fields[1]!="TRUE"
        if !hostOnly { c.Domain=domain }
        if expiry>0 { c.Expires=time.Unix(expiry,0) }
        origin:=&url.URL{Scheme:"https",Host:domain,Path:c.Path}
        result.jar.SetCookies(origin,[]*http.Cookie{&c})
        c.Domain=domain
        result.values[cookieID(domain,c.Path,c.Name)]=savedCookie{c,hostOnly}
    }
    return result,scanner.Err()
}

func (j *loginCookies) Cookies(u *url.URL) []*http.Cookie { return j.jar.Cookies(u) }
func (j *loginCookies) SetCookies(u *url.URL,cookies []*http.Cookie) {
    j.mu.Lock(); defer j.mu.Unlock()
    j.jar.SetCookies(u,cookies)
    for _,original:=range cookies {
        c:=*original
        hostOnly:=c.Domain==""
        if hostOnly { c.Domain=u.Hostname() }
        c.Domain=strings.ToLower(strings.TrimPrefix(c.Domain,"."))
        host:=strings.ToLower(u.Hostname())
        if c.Domain!="apple.com" && !strings.HasSuffix(c.Domain,".apple.com") || host!=c.Domain && !strings.HasSuffix(host,"."+c.Domain) { continue }
        if c.Path=="" || c.Path[0]!='/' {
            index:=strings.LastIndex(u.Path,"/"); c.Path="/"; if index>0 { c.Path=u.Path[:index] }
        }
        key:=cookieID(c.Domain,c.Path,c.Name)
        if c.MaxAge<0 || !c.Expires.IsZero() && c.Expires.Before(cookieNow()) { delete(j.values,key); continue }
        if c.MaxAge>0 { c.Expires=cookieNow().Add(time.Duration(c.MaxAge)*time.Second) }
        if strings.ContainsAny(c.Name+c.Value+c.Domain+c.Path,"\t\r\n") { continue }
        j.values[key]=savedCookie{c,hostOnly}
    }
}
func (j *loginCookies) commit(path string) error {
    j.mu.Lock(); defer j.mu.Unlock()
    var text strings.Builder
    text.WriteString("# Netscape HTTP Cookie File\n")
    keys:=make([]string,0,len(j.values)); for key:=range j.values { keys=append(keys,key) }; sort.Strings(keys)
    for _,key:=range keys {
        value:=j.values[key]; c:=value.cookie
        if !c.Expires.IsZero() && c.Expires.Before(cookieNow()) { continue }
        domain:=c.Domain; subdomains:="FALSE"; secure:="FALSE"; expiry:=int64(0)
        if !value.hostOnly { domain="."+domain; subdomains="TRUE" }
        if c.HttpOnly { domain="#HttpOnly_"+domain }
        if c.Secure { secure="TRUE" }; if !c.Expires.IsZero() { expiry=c.Expires.Unix() }
        _,_=fmt.Fprintf(&text,"%s\t%s\t%s\t%s\t%d\t%s\t%s\n",domain,subdomains,c.Path,secure,expiry,c.Name,c.Value)
    }
    return atomicWrite(path,[]byte(text.String()))
}
func (j *loginCookies) Save() error { return j.commit(j.staged) }
