啟動指令
export $(cat .env | xargs) && gunicorn -k eventlet -w 1 --bind 0.0.0.0:5200 --reload app:app