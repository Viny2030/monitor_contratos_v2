FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install streamlit
COPY . .
RUN mkdir -p /app/data
EXPOSE 8501
CMD ["sh", "-c", "streamlit run dashboard_v2.py --server.port=${PORT:-8501} --server.address=0.0.0.0 --server.headless=true"]
