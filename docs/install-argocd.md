
1-

```bash
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update
```

2- 
```bash
helm install argocd argo/argo-cd -n argocd --create-namespace \
  --version 10.10.5 -f platform/argocd/values.yaml #pin version to chart 10.10.5 with app-vesrion 3.5.5
 
kubectl -n argocd rollout status deploy/argocd-server
kubectl -n argocd get pods
```
