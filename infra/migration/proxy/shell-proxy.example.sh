# 先将地址改为新宿主机可被虚拟机访问的 HTTP 代理地址；按需 source 本文件。
export http_proxy="http://HOST_PROXY_IP:7890"
export https_proxy="$http_proxy"
export no_proxy="localhost,127.0.0.1,::1"
export NO_PROXY="$no_proxy"
