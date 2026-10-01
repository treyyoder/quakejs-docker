// Template: single-image build (static site, single service).
// Placeholders: quakejs-docker = image name (e.g. maga-gg). Dockerfile at repo root.
// Multi-arch: this image also runs on pi5 (arm64), so PLATFORMS builds linux/amd64 and linux/arm64 and
// pushes both under ONE tag (an OCI image index). Set it to '' to go back to an amd64-only build.
// Requires the arm64 QEMU binfmt on lovelace (apt package qemu-user-binfmt).
pipeline {
  agent any
  options { timestamps(); disableConcurrentBuilds(); buildDiscarder(logRotator(numToKeepStr: '20')) }
  environment {
    REGISTRY = 'registry.treyyoder.com'
    IMAGE    = "${REGISTRY}/quakejs-docker"
    TAG      = "${env.BUILD_NUMBER}"
    PLATFORMS = 'linux/amd64,linux/arm64'   // '' for amd64-only
  }
  stages {
    stage('Build image') {
      steps {
        script {
          if (env.PLATFORMS?.trim()) {
            // Builds every platform and pushes them under one tag; --provenance=false keeps the index to
            // real platforms (no unknown/unknown attestation entries for registry-ui or `compose pull`).
            sh 'docker buildx build --platform "$PLATFORMS" --provenance=false -t $IMAGE:$TAG -t $IMAGE:latest --push .'
          } else {
            sh 'docker build -t $IMAGE:$TAG -t $IMAGE:latest .'
          }
        }
      }
    }
    stage('Push image') {
      when { expression { !env.PLATFORMS?.trim() } }   // multi-arch builds already pushed
      steps { sh 'docker push $IMAGE:$TAG; docker push $IMAGE:latest' }
    }
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
