pipeline {
    agent any
    options {
        timestamps()
        disableConcurrentBuilds()
        skipDefaultCheckout(true)
        buildDiscarder(logRotator(numToKeepStr: '10'))
    }
    parameters {
        choice(name: 'ACTION', choices: ['PLAN', 'APPLY', 'DESTROY'], description: 'Local Hyper-V lab only. DESTROY deletes its three VMs/data disks.')
    }
    environment {
        ANSIBLE = '/opt/jenkins-mongodb-hyperv-venv/bin/ansible-playbook'
        LAB_TOKEN = "${env.BUILD_TAG}"
    }
    stages {
        stage('Checkout') {
            steps { deleteDir(); checkout scm }
        }
        stage('Preflight') {
            steps {
                sh '''
                    set -eu
                    test -r /var/lib/jenkins/mongodb-hyperv/lab.json
                    command -v ssh
                    command -v scp
                    command -v genisoimage
                    python3 -m unittest discover -s tests -v
                    "$ANSIBLE" -i localhost, ansible/install.yml --syntax-check
                    "$ANSIBLE" -i localhost, ansible/configure.yml --syntax-check
                '''
            }
        }
        stage('Plan local VMs') {
            steps {
                sh '''
                    set -eu
                    if [ "$ACTION" = DESTROY ]; then
                        python3 scripts/lab.py plan --intent DESTROY
                    else
                        python3 scripts/lab.py plan --intent APPLY
                    fi
                '''
                archiveArtifacts artifacts: 'build/plan.txt,build/plan.json', fingerprint: true
            }
        }
        stage('Review') {
            when { expression { params.ACTION != 'PLAN' } }
            steps {
                timeout(time: 30, unit: 'MINUTES') {
                    input message: "Review build/plan.txt. Execute ${params.ACTION} on local Hyper-V? DESTROY powers off and deletes only this lab's three owned VMs and data disks.", ok: 'Execute reviewed plan'
                }
            }
        }
        stage('Provision or destroy') {
            when { expression { params.ACTION != 'PLAN' } }
            steps { sh 'python3 scripts/lab.py execute' }
        }
        stage('Discover DHCP addresses') {
            when { expression { params.ACTION == 'APPLY' } }
            steps {
                sh 'python3 scripts/lab.py discover'
                archiveArtifacts artifacts: 'build/nodes.json,build/nodes.txt', fingerprint: true
            }
        }
        stage('Install MongoDB and verify replica set') {
            when { expression { params.ACTION == 'APPLY' } }
            steps {
                withCredentials([
                    string(credentialsId: 'mongodb-hyperv-keyfile', variable: 'MONGO_KEYFILE'),
                    usernamePassword(credentialsId: 'mongodb-hyperv-admin', usernameVariable: 'MONGO_ADMIN_USER', passwordVariable: 'MONGO_ADMIN_PASSWORD')
                ]) {
                    sh '''
                        set +x
                        set -eu
                        "$ANSIBLE" -i build/inventory.json ansible/install.yml
                        "$ANSIBLE" -i build/inventory.json ansible/configure.yml
                    '''
                }
                echo 'Verified: one primary, two secondaries, and a build probe replicated to all three.'
            }
        }
    }
    post {
        cleanup {
            // Seed ISOs contain only a public SSH key and boot configuration.
            // Host disk copies retain their seed; workspace copies are disposable.
            sh 'rm -rf build/seeds; rm -f build/mongo1.iso build/mongo2.iso build/mongo3.iso build/inventory.json'
        }
    }
}
