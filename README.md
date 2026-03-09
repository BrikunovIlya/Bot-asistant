# Bot-asistant
Bot-assistant project made during educational practice by student of the 3rd curse Brikunov Ilya

Запуск проекта 
python main.py

Запуск в Docker
docker build -t max-app .

docker run --rm \
  --name my-app \
  -p 8000:8000 \
  -v $(pwd)/.env:/app/.env \
  --env-file .env \
  max-app
