"""Build the reviewed gateway pinning contract over a fixed MIT CLIProxyAPI SDK."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen

from aigateway.core import BASE

VERSION='8.0.72-gateway.2'
REF='e96d4c4818851cf0e96d27b2bc8b67379e449d9e'
SOURCE_SHA='21ab830fea73afd9a462ef85f4c71ca0e971c6db5cee49f52521de47add4449a'
GO_SHA='ccfe7ae822d2c2f41f4fe7c956199967a4dac22d0ee0d2b2bfdb440aab3b20c5'
ROOT=Path(__file__).resolve().parents[1]


def download(url,path,expected):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o750)
    if not path.exists():
        fd,name=tempfile.mkstemp(prefix='download-',dir=path.parent)
        with os.fdopen(fd,'wb') as out,urlopen(url,timeout=60) as src:shutil.copyfileobj(src,out)
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=expected:raise ValueError('download checksum mismatch')
        os.link(name,path);Path(name).unlink();path.chmod(0o640)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ValueError('existing download checksum mismatch')
    return path


def main():
    os.umask(0o077)
    target=BASE/'tools/cliproxyapi'/VERSION;patch=ROOT/'bridge/account-binding.patch';patch_sha=hashlib.sha256(patch.read_bytes()).hexdigest()
    if target.exists():
        manifest=json.loads((target/'release.json').read_text())
        if manifest['patch_sha256']!=patch_sha or manifest['binary_sha256']!=hashlib.sha256((target/'cli-proxy-api').read_bytes()).hexdigest():
            raise ValueError('existing adapter differs; use a new version rather than overwrite')
        print(str(target/'cli-proxy-api'));return
    if shutil.disk_usage(BASE).free<3*1024**3:raise RuntimeError('disk space below 3GiB')
    archive=download('https://codeload.github.com/router-for-me/CLIProxyAPI/tar.gz/'+REF,
        BASE/'downloads/installers/cliproxyapi/8.0.72'/(REF+'-source.tar.gz'),SOURCE_SHA)
    go=BASE/'tools/go/1.26.10/go/bin/go'
    if not go.exists():
        package=download('https://go.dev/dl/go1.26.10.linux-amd64.tar.gz',BASE/'downloads/installers/go/1.26.10/go1.26.10.linux-amd64.tar.gz',GO_SHA)
        path=go.parents[2];path.mkdir(parents=True,mode=0o750)
        with tarfile.open(package) as tar:tar.extractall(path,filter='data')
    parent=Path(tempfile.mkdtemp(prefix='sdk-build-',dir=BASE/'tmp/ai-gateway'))
    with tarfile.open(archive) as tar:tar.extractall(parent,filter='data')
    source=parent/('CLIProxyAPI-'+REF)
    subprocess.run(['patch','--batch','--forward','-p1'],input=patch.read_bytes(),cwd=source,check=True,capture_output=True)
    env={**os.environ,'GOMODCACHE':str(BASE/'tools/go/cache/pkg/mod'),'GOCACHE':str(BASE/'tmp/ai-gateway/go-build-cache'),
         'GOMAXPROCS':'2','CGO_ENABLED':'0'}
    binary=parent/'cli-proxy-api'
    subprocess.run([str(go),'build','-p','2','-trimpath','-buildvcs=false','-ldflags','-s -w -X main.Version='+VERSION+' -X main.Commit='+REF,
                    '-o',str(binary),'./cmd/server'],cwd=source,env=env,check=True)
    target.mkdir(parents=True,mode=0o750)
    shutil.copy2(binary,target/'cli-proxy-api');(target/'cli-proxy-api').chmod(0o750)
    shutil.copy2(source/'LICENSE',target/'LICENSE');(target/'LICENSE').chmod(0o640)
    manifest={'version':VERSION,'upstream_commit':REF,'source_sha256':SOURCE_SHA,'patch_sha256':patch_sha,
              'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'go_version':'1.26.10','cgo':False,'pinning_contract':1}
    (target/'release.json').write_text(json.dumps(manifest,indent=2));(target/'release.json').chmod(0o640)
    print(str(target/'cli-proxy-api'))


if __name__=='__main__':main()
