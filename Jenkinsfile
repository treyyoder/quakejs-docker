// Template: single-image build (static site, single service).
// Placeholders: quakejs-docker = image name (e.g. maga-gg). Dockerfile at repo root.
pipeline {
  agent any
  options { timestamps(); disableConcurrentBuilds(); buildDiscarder(logRotator(numToKeepStr: '20')) }
  environment {
    REGISTRY = 'registry.treyyoder.com'
    IMAGE    = "${REGISTRY}/quakejs-docker"
    TAG      = "${env.BUILD_NUMBER}"
  }
  stages {
    stage('Build image') { steps { sh 'docker build -t $IMAGE:$TAG -t $IMAGE:latest .' } }
    stage('Push image')  { steps { sh 'docker push $IMAGE:$TAG; docker push $IMAGE:latest' } }
    stage('Redeploy stack') {
      steps {
        script {
          try {
            withCredentials([string(credentialsId: 'quakejs-docker-portainer-webhook', variable: 'PORTAINER_WEBHOOK')]) {
              sh 'curl -fsSk -X POST "$PORTAINER_WEBHOOK" && echo "Redeploy triggered." || echo "Webhook redeploy failed (non-fatal)."'
            }
          } catch (err) { echo "No webhook credential yet — skipping redeploy. (${err.message})" }
        }
      }
    }
  }
  post {
    success { echo "Pushed ${IMAGE}:${TAG}" }
    // NEVER `docker image prune`/`rm` the built images here: the agent shares the host daemon,
    // so it races with Portainer's webhook `compose pull` ("unable to lease content").
  }
}
