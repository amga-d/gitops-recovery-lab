1 - install helm in client 
2 - create gitea namespace 
```bash
kubectl create namespace gitea
```

3- install gitea via helm
```bash 
helm repo add gitea-charts https://dl.gitea.io/charts/
helm repo update


export GITEA_PW='gitea-password' # set the account password

helm install gitea gitea-charts/gitea -n gitea --create-namespace \
  --version 12.7.0 \ # pin 12.7.0 version for reproduciblity 
  -f platform/gitea/values.yaml \ # use the gitea-values.yaml configuration file 
  --set-string gitea.admin.password="$GITEA_PW"

kubectl -n gitea rollout status deploy/gitea
kubectl -n gitea get pods,svc
```
```
```
```
