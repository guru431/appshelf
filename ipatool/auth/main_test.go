package main

import (
    "bytes"
    "errors"
    "net/http"
    "net"
    "net/url"
    "os"
    "path/filepath"
    "strings"
    "testing"
    "time"

    "github.com/majd/ipatool/v2/pkg/appstore"
)

func TestMachineIdentityMatchesCppGuid(t *testing.T) {
    value,err:=(fixedMachine{guid:"000393123456"}).MacAddress()
    if err!=nil { t.Fatal(err) }
    parsed,err:=net.ParseMAC(value)
    if err!=nil || !bytes.Equal(parsed,[]byte{0,3,147,18,52,86}) { t.Fatal("device identity changed",err) }
}

func TestStateEncryptedAndAccountPasswordNeverPersisted(t *testing.T) {
    state:=&deviceState{path:filepath.Join(t.TempDir(),"adi-state")}
    secret:=[]byte("synthetic-device-provisioning-state")
    if err:=state.Set("account",[]byte(`{"password":"do-not-store"}`)); err!=nil { t.Fatal(err) }
    if _,err:=os.Stat(state.path); !os.IsNotExist(err) { t.Fatal("password-bearing account was persisted") }
    if err:=state.Set("anisette-v4",secret); err!=nil { t.Fatal(err) }
    raw,err:=os.ReadFile(state.path); if err!=nil { t.Fatal(err) }
    if bytes.Contains(raw,secret) { t.Fatal("state stored in plaintext") }
    info,_:=os.Stat(state.path); if info.Mode().Perm()!=0600 { t.Fatal("state permissions") }
    loaded,err:=state.Get("anisette-v4"); if err!=nil || !bytes.Equal(loaded,secret) { t.Fatal("state roundtrip",err) }
    raw[len(raw)-1]^=1
    if err=os.WriteFile(state.path,raw,0600); err!=nil { t.Fatal(err) }
    if _,err=state.Get("anisette-v4"); !errors.Is(err,errState) { t.Fatal("modified state accepted") }
}

func TestNetscapeCookiesRetainScopeAndSecondStepContinuity(t *testing.T) {
    previousClock:=cookieNow
    frozen:=time.Unix(1700000000,0)
    cookieNow=func() time.Time{return frozen}
    t.Cleanup(func(){cookieNow=previousClock})
    config:=t.TempDir()
    fixture:="# Netscape HTTP Cookie File\n#HttpOnly_.apple.com\tTRUE\t/\tTRUE\t0\tsession\tfixture\nauth.itunes.apple.com\tFALSE\t/auth\tTRUE\t0\thost\tlocal\n"
    if err:=os.WriteFile(filepath.Join(config,"cookies"),[]byte(fixture),0600); err!=nil { t.Fatal(err) }
    jar,err:=loadCookies(config,false); if err!=nil { t.Fatal(err) }
    origin,_:=url.Parse("https://auth.itunes.apple.com/auth/v1/native/fast/")
    if len(jar.Cookies(origin))!=2 { t.Fatal("cookies not loaded") }
    jar.SetCookies(origin,[]*http.Cookie{{Name:"step",Value:"two",Secure:true,HttpOnly:true,Path:"/auth"}})
    if err=jar.Save(); err!=nil { t.Fatal(err) }
    if err=os.Chtimes(jar.staged,frozen,frozen); err!=nil { t.Fatal(err) }
    next,err:=loadCookies(config,true); if err!=nil { t.Fatal(err) }
    if len(next.Cookies(origin))!=3 { t.Fatal("2FA continuity lost") }
    other,_:=url.Parse("https://buy.itunes.apple.com/auth")
    if len(next.Cookies(other))!=1 { t.Fatal("host-only cookie escaped") }
    next.SetCookies(origin,[]*http.Cookie{{Name:"step",Path:"/auth",MaxAge:-1}})
    if err=next.Save(); err!=nil { t.Fatal(err) }
    if err=os.Chtimes(next.staged,frozen,frozen); err!=nil { t.Fatal(err) }
    third,err:=loadCookies(config,true); if err!=nil || len(third.Cookies(origin))!=2 { t.Fatal("cookie removal lost",err) }
}

func TestFailedAccountImportRestoresOriginalCookies(t *testing.T) {
    config:=t.TempDir()
    target:=filepath.Join(config,"cookies")
    before:=[]byte("# Netscape HTTP Cookie File\n.apple.com\tTRUE\t/\tTRUE\t0\tsession\toriginal\n")
    if err:=os.WriteFile(target,before,0600); err!=nil { t.Fatal(err) }
    jar,err:=loadCookies(config,false); if err!=nil { t.Fatal(err) }
    origin,_:=url.Parse("https://auth.itunes.apple.com/")
    jar.SetCookies(origin,[]*http.Cookie{{Name:"session",Value:"new",Domain:"apple.com",Path:"/"}})
    cpp:=filepath.Join(config,"reject-import")
    if err=os.WriteFile(cpp,[]byte("#!/bin/sh\nexit 1\n"),0700); err!=nil { t.Fatal(err) }
    if _,err=persistAccount(cpp,config,jar,[]byte(`{}`)); err==nil { t.Fatal("failed import accepted") }
    after,_:=os.ReadFile(target)
    if !bytes.Equal(before,after) { t.Fatal("working cookies changed after failed import") }
}

func TestErrorsExposeOnlyAllowedMetadata(t *testing.T) {
    exit,code,message:=errorCode(errors.New("secret-cookie=private-token: HTTP 403"))
    if exit!=1 || code!="error" || strings.Contains(message,"private") || !strings.Contains(message,"HTTP 403") { t.Fatal("unsafe error output") }
    exit,code,_=errorCode(appstore.ErrAuthCodeRequired)
    if exit!=4 || code!="auth_code_required" { t.Fatal("2FA challenge misclassified") }
}
