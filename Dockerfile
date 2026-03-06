FROM python:3.10.12

ENV http_proxy http://@proxy.amo.murman.ru:8080
ENV https_proxy http://@proxy.amo.murman.ru:8080
ENV no_proxy 127.0.0.1, 172/21/0/0, localhost, .local,.gov-murman.ru,.amo-murman.ru,172.21.251.157,/var/run/docker.sock
ENV HTTP_PROXY http://@proxy.amo.murman.ru:8080
ENV HTTPS_PROXY http://@proxy.amo.murman.ru:8080
ENV NO_PROXY 127.0.0.1, 172/21/0/0, localhost, .local,.gov-murman.ru,.amo-murman.ru,172.21.251.157,/var/run/docker.sock


WORKDIR /max

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]

