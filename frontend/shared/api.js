export function createApi(getSession) {
return async function api(path,{method='GET',body,signal,keepalive=false,responseType='json'}={}){
  const headers={Accept:responseType==='blob'?'text/csv':'application/json'};if(body!==undefined)headers['Content-Type']='application/json';if(method!=='GET')headers['X-CSRF-Token']=getSession()?.csrf_token||'';
  const response=await fetch(path,{method,headers,credentials:'same-origin',...(body!==undefined?{body:JSON.stringify(body)}:{}),...(signal?{signal}:{}),keepalive});
  if(response.status===401){location.assign('/login?next=%2F');throw new Error('로그인이 만료되었습니다. 다시 로그인해주세요.');}
  if(response.ok&&responseType==='blob')return response.blob();
  let data={};if(response.status!==204){try{data=await response.json();}catch{if(response.ok)throw new Error('서버의 응답 형식을 확인해주세요.');}}
  if(!response.ok){const error=new Error(typeof data.error==='string'?data.error:data.error?.message||data.message||`요청을 처리하지 못했습니다 (${response.status}).`);error.status=response.status;error.data=data;throw error;}return data;
};
}
