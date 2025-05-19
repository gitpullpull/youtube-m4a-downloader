FastAPIとytdlpでyoutubeからm4aで音声をDL出来ます。
オプションでサムネイルを付けることができます。

vercelで動かす場合は、cookieを適切に渡してローカルからデプロイする必要があります。
POTOKENを使う方式に切り替え予定

###使い方
```
pip requirements.txt
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```
