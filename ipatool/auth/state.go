package main

import (
    "crypto/aes"
    "crypto/cipher"
    "crypto/pbkdf2"
    "crypto/rand"
    "crypto/sha256"
    "errors"
    "io"
    "os"
    "path/filepath"
    "strings"
)

var errState=errors.New("Apple device state storage unavailable")
var stateMagic=[]byte("ASADI1")
var machineIDPath="/etc/machine-id"
type deviceState struct { path string }

func atomicWrite(path string,data []byte) error {
    file,err:=os.CreateTemp(filepath.Dir(path),".auth-*")
    if err!=nil { return err }
    temp:=file.Name()
    defer os.Remove(temp)
    if err=file.Chmod(0600); err==nil { _,err=file.Write(data) }
    if err==nil { err=file.Sync() }
    closeErr:=file.Close()
    if err==nil { err=closeErr }
    if err!=nil { return err }
    return os.Rename(temp,path)
}

func stateCipher(salt []byte) (cipher.AEAD,error) {
    identity,err:=os.ReadFile(machineIDPath)
    if err!=nil || len(strings.TrimSpace(string(identity)))<16 { return nil,errState }
    key,err:=pbkdf2.Key(sha256.New,string(identity)+"appshelf-native-auth-v1",salt,100000,32)
    if err!=nil { return nil,errState }
    block,err:=aes.NewCipher(key); clear(key)
    if err!=nil { return nil,errState }
    return cipher.NewGCM(block)
}

func (s *deviceState) Get(key string) ([]byte,error) {
    if key!="anisette-v4" { return nil,os.ErrNotExist }
    file,err:=os.Open(s.path)
    if err!=nil { if os.IsNotExist(err) { return nil,err }; return nil,errState }
    defer file.Close()
    info,err:=file.Stat()
    if err!=nil || info.Size()>2<<20 || info.Mode().Perm()&0077!=0 { return nil,errState }
    raw,err:=io.ReadAll(io.LimitReader(file,2<<20))
    if err!=nil || len(raw)<6+16+12+16 || string(raw[:6])!=string(stateMagic) { return nil,errState }
    engine,err:=stateCipher(raw[6:22]); if err!=nil { return nil,errState }
    plain,err:=engine.Open(nil,raw[22:34],raw[34:],stateMagic)
    if err!=nil { return nil,errState }
    return plain,nil
}
// usable reports whether login may proceed. Unreadable existing state (another machine-id, group/other
// permissions, corruption) is no reason to refuse: upstream ignores it and provisions anew, and a successful
// login atomically replaces the file. Only a missing machine-id would make that save fail after Apple's answer.
func (s *deviceState) usable() error {
    if _,err:=s.Get("anisette-v4"); err==nil || os.IsNotExist(err) { return nil }
    if _,err:=stateCipher(make([]byte,16)); err!=nil { return errState }
    event("state_reset",0)
    return nil
}
func (s *deviceState) Set(key string,data []byte) error {
    // Upstream supplies an account JSON containing the password. Never persist it.
    // The successful Login result is imported by the existing password-free C++ writer.
    if key=="account" { return nil }
    if key!="anisette-v4" || len(data)>1<<20 { return errState }
    salt:=make([]byte,16); nonce:=make([]byte,12)
    if _,err:=rand.Read(salt); err!=nil { return errState }
    if _,err:=rand.Read(nonce); err!=nil { return errState }
    engine,err:=stateCipher(salt); if err!=nil { return errState }
    out:=append(append(append([]byte{},stateMagic...),salt...),nonce...)
    out=engine.Seal(out,nonce,data,stateMagic)
    if atomicWrite(s.path,out)!=nil { return errState }
    return nil
}
func (s *deviceState) Remove(key string) error {
    if key!="anisette-v4" { return os.ErrNotExist }
    return os.Remove(s.path)
}
