export async function request(path,options={}) {
 const token=localStorage.getItem('research-token');
 const response=await fetch('/api'+path,{...options,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{}),...options.headers}});
 if(!response.ok) await failure(response,path);
 return response.headers.get('content-type')?.includes('text/csv')?response.blob():response.json();
}
async function failure(response,path){
 const x=await response.json().catch(()=>({detail:'网络错误，请重试'}));
 if(response.status===401 && !path.includes('login')){localStorage.clear();location.reload();}
 const detail=x.detail;
 const message=typeof detail==='string'?detail:(detail?.message||JSON.stringify(detail));
 const error=new Error(message);
 error.detail=detail;
 error.status=response.status;
 throw error;
}
export const post=(path,body)=>request(path,{method:'POST',body:JSON.stringify(body??{})});
export async function downloadFile(path){
 const token=localStorage.getItem('research-token');
 const response=await fetch('/api'+path,{headers:token?{Authorization:'Bearer '+token}:{}});
 if(!response.ok) await failure(response,path);
 return {blob:await response.blob(),headers:response.headers};
}
export async function upload(path,file){
 const token=localStorage.getItem('research-token');
 const response=await fetch('/api'+path,{method:'POST',body:file,headers:{'Content-Type':'application/zip',...(token?{Authorization:'Bearer '+token}:{})}});
 if(!response.ok) await failure(response,path);
 return response.json();
}
