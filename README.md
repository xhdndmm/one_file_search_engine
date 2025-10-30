# One_File_Search_Engine
- v1.0效果图  
<img src="./屏幕截图_20251030_184240.png" width="600"><img>
<img src="./屏幕截图_20251030_184415.png" width="600"><img>  
![](https://skillicons.dev/icons?i=docker,flask,py,sqlite)

## 这是什么？
这是一个简易的轻量级的单文件搜索引擎（只需要准备Python，安装Flask。整个项目源码只有一个文件）  
使用Python编写，SQLite数据库，Flask Web服务，支持爬取网页，记录网页信息，并根据关键信息搜索的功能。

## 如何安装并使用？
准备[Python3](https://www.python.org/downloads/release/python-3128/)环境  
克隆存储库并安装依赖：
```shell
git clone https://github.com/xhdndmm/one_file_search_engine && cd one_file_search_engine && pip install -r requirements.txt
```
- 注：旧版本可以在[这里](https://github.com/xhdndmm/one_file_search_engine/releases)下载  

启动程序：
```shell
python3 src/main.py
```
你可以使用tmux守护进程，或者gunicorn。

## 问题反馈及贡献
你可以在[这里](https://github.com/xhdndmm/one_file_search_engine/issues)反馈问题，欢迎你来提供宝贵的意见。  
同样你可以提交PR请求，为项目添砖加瓦。

## 其他
请遵守GPLV3开源协议