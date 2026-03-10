## БОТ-ассистент

Бот ассистент для помощи гражданам в поиске информации по социальным льготам.

Запуск проекта 
python main.py

Python: 3.13-slim
OC: Windows/Linux

Запуск в Docker
docker build -t max-app .

docker run --rm \
  --name my-app \
  -p 8000:8000 \
  -v $(pwd)/.env:/app/.env \
  --env-file .env \
  max-app
