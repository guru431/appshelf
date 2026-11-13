// Appshelf adapter for the pinned upstream native Apple authentication engine.
// Passwords stay on stdin/in memory. Only the C++ client writes account files.
package main

import (
    "bufio"
    "bytes"
    "context"
    "crypto/rand"
    "encoding/hex"
    "encoding/json"
    "errors"
    "fmt"
    "io"
    "log"
    "log/syslog"
    "net"
    "os"
    "os/exec"
    "path/filepath"
    "regexp"
    "strings"
    "time"

    "github.com/majd/ipatool/v2/internal/sap/assets"
    sapmachine "github.com/majd/ipatool/v2/internal/sap/machine"
    "github.com/majd/ipatool/v2/internal/store/profile"
    "github.com/majd/ipatool/v2/pkg/appstore"
    "github.com/majd/ipatool/v2/pkg/util/operatingsystem"
    "github.com/rs/zerolog"
)

type fixedMachine struct{ guid, home string }
func (m fixedMachine) MacAddress() (string,error) {
    raw,err:=hex.DecodeString(m.guid)
    if err!=nil || len(raw)!=6 { return "",errors.New("invalid device identity") }
    return net.HardwareAddr(raw).String(),nil
}
func (m fixedMachine) HomeDirectory() string { return m.home }
func (m fixedMachine) ReadPassword(int) ([]byte,error) { return nil,errors.New("stdin required") }

func event(stage string, status int) {
    writer,err:=syslog.New(syslog.LOG_NOTICE|syslog.LOG_USER,"ipatool-auth")
    if err==nil { defer writer.Close(); _=writer.Notice(fmt.Sprintf("appshelf: Native Apple auth stage=%s http=%d",stage,status)) }
}
func fail(exit int, code,message string) {
    _=json.NewEncoder(os.Stdout).Encode(map[string]string{"error":code,"message":message})
    os.Exit(exit)
}

func errorCode(err error) (int,string,string) {
    if errors.Is(err,appstore.ErrAuthCodeRequired) { return 4,"auth_code_required","two-factor authentication code required" }
    if errors.Is(err,errState) { return 1,"save_failed","failed to save Apple device state" }
    text:=strings.ToLower(err.Error())
    if strings.Contains(text,"account is disabled") { return 1,"account_disabled","account is disabled" }
    if strings.Contains(text,"incorrect") || strings.Contains(text,"invalid credentials") || strings.Contains(text,"did not complete verification") {
        return 1,"invalid_credentials","invalid Apple ID password or verification code"
    }
    stage:="exchange"
    for _,value:=range []string{"assets","anisette","provision","signature","bag","runtime"} {
        if strings.Contains(text,value) { stage=value; break }
    }
    status:=0
    if match:=regexp.MustCompile(`(?i)HTTP ([1-5][0-9]{2})`).FindStringSubmatch(err.Error()); len(match)>1 {
        _,_=fmt.Sscanf(match[1],"%d",&status)
    }
    event(stage,status)
    return 1,"error",fmt.Sprintf("native Apple authentication failed (stage=%s, HTTP %d)",stage,status)
}

func main() {
    defer func(){ if recover()!=nil { event("panic",0); fail(1,"error","native Apple authentication engine failed") } }()
    log.SetOutput(io.Discard)
    zerolog.SetGlobalLevel(zerolog.Disabled)
    if len(os.Args)==2 && os.Args[1]=="--help" {
        fmt.Println("Appshelf native Apple login: password via stdin; --prepare preloads verified assets")
        return
    }
    exe,err:=os.Executable()
    if err!=nil { fail(1,"error","native authentication location unavailable") }
    if os.Geteuid()==0 { fail(1,"usage","run authentication as the application user") }
    if os.Getenv("XDG_CACHE_HOME")=="" {
        // Root-owned, beside the binary: upstream checks a cached library's SHA-256 and only then loads it,
        // so the application user must not be able to replace it in between. deploy/build-ipatool.sh prepares it.
        cache:=filepath.Join(filepath.Dir(exe),"ipatool-auth-cache")
        if err=os.MkdirAll(cache,0700); err!=nil { fail(1,"save_failed","native authentication cache unavailable") }
        _=os.Setenv("XDG_CACHE_HOME",cache)
    }
    if len(os.Args)==2 && os.Args[1]=="--prepare" {
        ctx,cancel:=context.WithTimeout(context.Background(),10*time.Minute); defer cancel()
        bundle,err:=assets.Load(ctx)
        if err==nil { _,err=profile.Load(ctx) }
        if err==nil {
            identity:=make([]byte,16)
            _,err=rand.Read(identity)
            if err!=nil { fail(1,"error","device identity preparation failed") }
            runtime,openErr:=sapmachine.OpenModernADI(ctx,bundle,identity,nil)
            err=openErr
            if runtime!=nil { _=runtime.Close() }
        }
        if err!=nil { exit,code,message:=errorCode(err); fail(exit,code,message) }
        fmt.Println(`{"success":true,"prepared":true}`)
        return
    }
    if len(os.Args)==3 && os.Args[1]=="--store-headers" {
        printCommerceHeaders(filepath.Join(filepath.Dir(exe),"ipatool"),os.Args[2])
        return
    }
    values:=map[string]string{}
    stdinPassword:=false
    for i:=1;i<len(os.Args);i++ {
        arg:=os.Args[i]
        switch arg {
        case "auth","login":
        case "--password-stdin": stdinPassword=true
        case "-e","--email","--auth-code","--format":
            if i+1>=len(os.Args) { fail(1,"usage","missing authentication argument") }
            i++; values[arg]=os.Args[i]
        default: fail(1,"usage","unsupported native authentication argument")
        }
    }
    email:=values["-e"]; if email=="" { email=values["--email"] }
    if email=="" || !stdinPassword { fail(1,"usage","email and password stdin are required") }
    reader:=bufio.NewReader(io.LimitReader(os.Stdin,65537))
    password,readErr:=reader.ReadString('\n')
    if readErr!=nil && readErr!=io.EOF { fail(1,"usage","password stdin could not be read") }
    password=strings.TrimSuffix(strings.TrimSuffix(password,"\n"),"\r")
    if password=="" || len(password)>65536 { fail(1,"usage","invalid password input") }
    // Match the outer ipatool timeout while keeping every password-bearing process bounded.
    timer:=time.AfterFunc(110*time.Second,func(){ fail(1,"timeout","native Apple authentication timed out") })
    defer timer.Stop()
    cpp:=filepath.Join(filepath.Dir(exe),"ipatool")
    command:=exec.Command(cpp,"--format","json","device-info")
    device,err:=command.Output()
    var identity struct{ GUID string `json:"guid"` }
    if err!=nil || json.Unmarshal(device,&identity)!=nil { fail(1,"bad_device_mac","device identity unavailable") }
    if raw,decodeErr:=hex.DecodeString(identity.GUID); decodeErr!=nil || len(raw)!=6 { fail(1,"bad_device_mac","invalid device identity") }
    home:=os.Getenv("HOME")
    if !filepath.IsAbs(home) { fail(1,"usage","absolute account HOME required") }
    config:=filepath.Join(home,".ipatool")
    if err=os.MkdirAll(config,0700); err!=nil { fail(1,"save_failed","account directory unavailable") }
    jar,err:=loadCookies(config,values["--auth-code"]!="")
    if err!=nil { fail(1,"save_failed","authentication cookies unavailable") }
    keychain:=&deviceState{path:filepath.Join(config,"adi-state")}
    if keychain.usable()!=nil { fail(1,"save_failed","Apple device state cannot be saved on this machine") }
    client:=appstore.NewAppStore(appstore.Args{Keychain:keychain,CookieJar:jar,
        Machine:fixedMachine{identity.GUID,home},OperatingSystem:operatingsystem.New()})
    event("start",0)
    result,err:=client.Login(appstore.LoginInput{Email:email,Password:password,AuthCode:values["--auth-code"]})
    password=""
    if err!=nil { exit,code,message:=errorCode(err); fail(exit,code,message) }
    result.Account.Password=""
    payload,err:=json.Marshal(result.Account)
    if err!=nil { fail(1,"save_failed","authenticated account unavailable") }
    public,err:=persistAccount(cpp,config,jar,payload)
    clear(payload)
    if err!=nil { fail(1,"save_failed","Apple authenticated, but saving the account failed") }
    event("complete",200)
    _,_=os.Stdout.Write(public)
}

func persistAccount(cpp,config string,jar *loginCookies,payload []byte) ([]byte,error) {
    target:=filepath.Join(config,"cookies")
    old,err:=os.ReadFile(target)
    existed:=err==nil
    if err!=nil && !os.IsNotExist(err) { return nil,err }
    if err=jar.commit(target); err!=nil { return nil,err }
    command:=exec.Command(cpp,"--format","json","auth","import")
    command.Stdin=bytes.NewReader(payload)
    public,err:=command.Output()
    if err!=nil {
        var rollback error
        if existed { rollback=atomicWrite(target,old) } else { rollback=os.Remove(target) }
        return nil,errors.Join(err,rollback)
    }
    var info struct { Success bool `json:"success"` }
    if json.Unmarshal(public,&info)!=nil || !info.Success { return nil,errors.New("invalid account import response") }
    return public,nil
}
