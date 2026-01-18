FROM python:3.10-slim

# Install OpenJDK 17 (Required for SonarScanner)
RUN apt-get update && \
    apt-get install -y openjdk-17-jre-headless curl unzip && \
    rm -rf /var/lib/apt/lists/*

# Install SonarScanner
ARG SONAR_SCANNER_VERSION=5.0.1.3006
ENV SONAR_SCANNER_HOME=/opt/sonar-scanner

RUN curl -L -o /tmp/sonar-scanner.zip https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-${SONAR_SCANNER_VERSION}-linux.zip && \
    unzip /tmp/sonar-scanner.zip -d /opt && \
    mv /opt/sonar-scanner-${SONAR_SCANNER_VERSION}-linux ${SONAR_SCANNER_HOME} && \
    rm /tmp/sonar-scanner.zip

ENV PATH="${SONAR_SCANNER_HOME}/bin:${PATH}"

# Install Python deps
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy source
COPY src/ /app/src/

# Entrypoint
COPY src/main.py /app/main.py
ENTRYPOINT ["python", "/app/main.py"]
