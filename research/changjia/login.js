$("#login").validate({
    errorElement: 'div',
    rules: {
        email_account: {
            required: true,
            maxlength: 100
        },
        password: {
            required: true,
            maxlength: 100
        }
    },
    messages: {
        email_account: {
            required: "此欄位必填",
            maxlength: "長度限制為100字元"
        },
        password: {
            required: "此欄位必填",
            maxlength: "長度限制為100字元"
        },
    }
});

function registerClick(){
	window.location.href="../../register/"
}

$('#login').keypress(function(e) {
    code = e.keyCode ? e.keyCode : e.which; // in case of browser compatibility
    if(code == 13) {       
        loginClick("");
    }
});

function preloginToLogin()
{
    window.location.href="/login";
}

function loginClick(action){
    loginClickWithStatus(action);    
}

function loginClickWithStatus(status){
    //檢查input欄位規則
    if ( !$("#login").valid() ) return;
	let type = "password";
    let form_data = $("#login").serialize();
    $.ajax({
        url: 'ajax/login.php',
        data: form_data + "&type=" + type,             
        type: 'post',
        dataType: 'json',
        success: function(response){
            if(response.success){
                Swal.fire({
                    icon: 'success',
                    title: '登入成功。',
                    confirmButtonColor: '#0CB4B5',
                }).then((result) => {
                    if(objType(response.relocate)!=null){
                        if(typeof (response.relocate) === "object") {
                            // 用post方法提交表單資料
                            redirectToPost(response.relocate.location,response.relocate.data)
                        } else {
                            window.location.href = response.relocate;
                        }
                        
                      }else{
                        window.location.href="../My/"+status;
                      }
                });
            }else{
                Swal.fire({
                    icon: 'error',
                    title: response.msg,
                    confirmButtonColor: '#0CB4B5',
                });
            }
        }, error: function (request, error) {
                console.log(arguments);
                alert(" (R)Login Error: " + error);
            }


    });
}


function objType(s) {    
    if(!s){
        return null;
    }else{
       return s;
    }   
}

function reloadCaptcha() {
    var captchaContainer = document.getElementById('captchaContainer');
    var captchaImage = captchaContainer.querySelector('img');
    captchaImage.src = 'ajax/captcha.php?' + new Date().getTime();
}


function checkEnter(event) {
  if (event.keyCode === 13) {
      // Enter 鍵被按下，觸發登入操作
      loginClick("");
  }
}

function getActionParam()
{
    var paramObj = getparam();
    var action = paramObj['action']!= null ? paramObj['action'] : "";
    return action;
}


function getparam() {
    var js = document.getElementsByTagName("script") ;
    //得到当前引用a。js一行的script并把src用分成数组
    var arraytemp = js[js.length - 1].src.split('?');
    var obj = new Object();
    //如果不带參数,则不执行下面的代码
    if (arraytemp. length > 1) {
        var params = arraytemp[1].split('&');
        for (var i = 0; i < params.length; i++) {
            var parm = params[i].split("=");
            //将key和value定义给obj
            obj [parm[0]] = parm[1];
            // alert(parm[0] + "=" + parm[1]);
        }
    }
    return obj;
}
   



goToMemberIfLogin();
//確認登入狀態
function goToMemberIfLogin(){
    // $.ajax({
    //     url: '../reconfirm/ajax/get_reconfirm_member.php',
    //     type: 'post',
    //     dataType: 'json',
    //     success: function(response){
    //         if (response.success) {
    //             if ( response.status == '1' ){
    //                     window.location.href= "/member";
    //             }
    //         } else {
    //             Swal.fire({
    //                 icon: "error",
    //                 title: response.msg,
    //                 allowOutsideClick: false,
    //                 timer: 1000,
    //                 showConfirmButton: false
    //             }).then((swalResult) => {
    //                 window.location.href= domain_url + "/";
    //             });
    //         }
    //     },
    //     error: function(response){
    //         Swal.fire({
    //             icon: "error",
    //             title: "error",
    //             allowOutsideClick: false,
    //            // timer: 1000,
    //             showConfirmButton: true,
    //             html: response.responseText
    //         });

    //     }
    // });
}

function redirectToPost(url, data) {
    let form = document.createElement("form");
    form.method = "POST";
    form.action = url;


    for (let key in data) {
        if (data.hasOwnProperty(key)) {
            let input = document.createElement("input");
            input.type = "hidden";
            input.name = key;
            input.value = data[key];
            form.appendChild(input);
        }
    }

    document.body.appendChild(form);
    form.submit();


    
}
