# Cliente Python mínimo para a API de agentes da IAEdu

Um único ficheiro Python (`iaedu_client.py`) para enviar mensagens a um agente da plataforma **IAEdu** a partir de código, e ler a resposta que chega em *streaming*.

> **Projecto independente.** Este repositório não tem qualquer relação oficial com a IAEdu nem com a FCT/FCCN, não é mantido por elas e não tem o seu aval. O formato da API foi observado na prática e pode mudar sem aviso.

Conteúdo:

| Ficheiro | Para que serve |
|---|---|
| `iaedu_client.py` | O cliente: classe `IaeduClient` |
| `tests/` | Testes sem rede (a rede fica bloqueada durante a execução) |
| `examples/smoke.py` | Primeiro teste ao vivo contra a API real |
| `requirements.txt` | Dependências com versões fixadas |

---

## 1. Requisitos

- **Python 3.10 ou superior** (testado em 3.12 e 3.14). A versão mínima vem da biblioteca `requests`, que deixou de suportar o 3.9.
- Uma conta na IAEdu com, pelo menos, um agente criado.
- Acesso à internet para `api.iaedu.pt` (HTTPS, porta 443).

Confirmar a versão:

```bash
python3 --version
```

---

## 2. Instalação

Num terminal, dentro da pasta do repositório:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows (PowerShell): .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

O ambiente virtual (`.venv`) mantém as dependências isoladas do resto do sistema. Sempre que abrir um terminal novo, volte a activá-lo com a linha `source` (ou o equivalente em Windows).

Para confirmar que a instalação está bem, corra os testes (não precisam de chave nem de rede):

```bash
python -m unittest discover -v
```

O resultado esperado termina em `OK`.

---

## 3. Obter a chave e os identificadores na IAEdu

São precisos três valores: a **chave de API**, o **identificador do agente** e o **identificador do canal**.

1. Entre na IAEdu e abra a **conversa modelo** do agente que quer usar.
2. Clique no **ícone da engrenagem**, no canto superior direito.
3. Escolha **«Informação da API»**.
4. Copie os valores apresentados: chave de API, identificador do agente e identificador do canal.

Pontos a saber (observados na prática; podem mudar):

- **A chave é por agente.** Uma chave só autentica o seu próprio agente: usada com outro agente, a API responde `401`.
- **A chave pode não ser regenerável.** Na altura em que isto foi escrito, o portal mostrava sempre a mesma chave e não oferecia forma de a substituir. Trate-a como permanente: se a expuser, pode não haver maneira de a anular.

---

## 4. Configuração por variáveis de ambiente

O cliente lê a configuração do ambiente. **Nada de secreto fica escrito no código.**

| Variável | Obrigatória | Valor por defeito | Uso |
|---|---|---|---|
| `IAEDU_API_KEY` | Sim | — | Chave de API do agente |
| `IAEDU_BASE_URL` | Não | `https://api.iaedu.pt/agent-chat/api/v1` | Endereço base da API |
| `IAEDU_AGENT_ID` | Só no exemplo | — | Identificador do agente |
| `IAEDU_CHANNEL_ID` | Só no exemplo | — | Identificador do canal |

Na sessão actual do terminal (macOS / Linux):

```bash
export IAEDU_API_KEY="<a-sua-chave>"
export IAEDU_AGENT_ID="<agent-id>"
export IAEDU_CHANNEL_ID="<channel-id>"
```

Em Windows (PowerShell):

```powershell
$env:IAEDU_API_KEY = "<a-sua-chave>"
$env:IAEDU_AGENT_ID = "<agent-id>"
$env:IAEDU_CHANNEL_ID = "<channel-id>"
```

> **Nunca versione a chave.** Não a escreva em ficheiros de código, não a cole em *issues* nem em conversas, e não faça *commit* de ficheiros `.env`. O `.gitignore` deste repositório já exclui `.env` e `.venv/`, mas a protecção principal é não a pôr em ficheiros dentro da pasta do projecto. Se usar um ficheiro de configuração, guarde-o fora do repositório e com permissões restritas (`chmod 600`).

O cliente recusa endereços `http://` sem TLS, porque a chave seguiria em claro. Pela mesma razão, **não segue redireccionamentos**: qualquer resposta `3xx` dá erro, para a chave nunca ser reenviada para outro endereço.

> **Não ligue o registo de depuração HTTP** (por exemplo `http.client.HTTPConnection.debuglevel = 1`, ou o nível `DEBUG` do `urllib3` com cabeçalhos): imprime os cabeçalhos do pedido, e a chave vai num deles.

---

## 5. Primeiro teste

Com as três variáveis definidas, a partir da raiz do repositório:

```bash
python examples/smoke.py
```

O exemplo envia a mensagem «Responde apenas: OK» e mostra o código HTTP, a latência, o texto montado e as linhas cruas do *stream*. Códigos de saída: `0` correu bem; `1` a chamada falhou; `2` falta configuração; `3` limite de taxa atingido.

> Se colar esta saída num *issue* ou num fórum, **apague antes o identificador do agente e o do canal**: aparecem nas primeiras linhas.

Uso no seu próprio código:

```python
from iaedu_client import IaeduClient

client = IaeduClient()  # lê IAEDU_API_KEY e IAEDU_BASE_URL

r = client.chat(agent_id="<agent-id>", channel_id="<channel-id>", message="Olá")
print(r["status"], r["text"])

# Continuar a mesma conversa: reutilizar o thread_id devolvido.
r2 = client.chat("<agent-id>", "<channel-id>", "E a seguir?", thread_id=r["thread_id"])
```

Parâmetros opcionais de `chat()`: `thread_id`, `user_info` (texto JSON ou `dict`), `user_id`, `user_context`, `image` (ficheiro aberto em modo binário) e `timeout` (segundos, por defeito 120).

> **Dados pessoais.** O que puser em `message`, `user_info`, `user_id` ou `user_context` é enviado para a IAEdu. Se incluir dados pessoais de terceiros, a responsabilidade pelo seu tratamento, à luz do RGPD, é de quem usa o cliente. Na dúvida, não os envie, ou use identificadores que não permitam reconhecer a pessoa.

---

## 6. Como se lê o *stream*

A API responde com **uma linha JSON por evento**. O cliente lê-as todas e devolve um dicionário. Os eventos observados são:

| `type` | Conteúdo | O que o cliente faz |
|---|---|---|
| `start` | Aviso de início | Ignora |
| `token` | Um pedaço do texto | Junta ao texto final |
| `message` | Texto completo e metadados (modelo, motivo de paragem) | Usa como texto se não tiver havido `token`; guarda os metadados |
| `done` | Fim, com identificador da mensagem | Guarda o identificador |
| `error` | Descrição do erro | Guarda em `error` |

Campos do resultado: `text`, `raw_chunks` (linhas cruas), `status`, `latency_ms`, `rate_limited`, `thread_id`, `model`, `provider`, `message_id`, `stop_reason` e `error`.

Se o formato mudar, o cliente cai num modo defensivo (aceita linhas `data:` de SSE, outras formas de JSON e texto simples). Em caso de dúvida, olhe para `raw_chunks`: é exactamente o que o servidor enviou.

O cliente espera pelo fim do *stream* antes de devolver. Para mostrar o texto à medida que chega seria preciso iterar as linhas por conta própria; não está incluído, para manter o código pequeno.

---

## 7. Limites de taxa

**Os limites da API não estão documentados** e não são conhecidos. O cliente marca `rate_limited: True` quando vê algum destes sinais:

- código HTTP `429`;
- cabeçalho `Retry-After`;
- cabeçalho `X-RateLimit-Remaining` ou `RateLimit-Remaining` a `0`.

Se `rate_limited` vier `True`, pare: não repita a chamada de imediato e, se houver `Retry-After`, espere pelo menos esse tempo antes de voltar a tentar.

A IAEdu é infra-estrutura académica partilhada. Use-a para o fim a que se destina, com chamadas espaçadas, e **respeite os termos e a política de utilização responsável da IAEdu**.

---

## 8. Resolução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| `Missing API key` | `IAEDU_API_KEY` não definida neste terminal | Repetir o `export` (ou `$env:`) na mesma sessão |
| `401` | Chave errada, ou chave de outro agente | Copiar de novo a chave e o identificador do **mesmo** agente |
| `404` | Identificador do agente errado | Confirmar o valor em «Informação da API» |
| `429` | Limite de taxa | Esperar (ver `Retry-After`) e espaçar as chamadas |
| `Refusing non-HTTPS base URL` | `IAEDU_BASE_URL` começa por `http://` | Usar `https://` ou remover a variável |
| `Refusing to follow HTTP 3xx redirect` | O endereço base mudou ou está errado | Confirmar `IAEDU_BASE_URL`; não mudar o código para seguir redireccionamentos |
| `Network error` / tempo esgotado | Sem rede, *proxy* ou firewall | Testar o acesso a `https://api.iaedu.pt` no browser; aumentar `timeout` |
| `ModuleNotFoundError: requests` | Ambiente virtual não activado | `source .venv/bin/activate` e repetir |
| Texto vazio com `status` 200 | Formato do *stream* diferente do esperado | Inspeccionar `raw_chunks` e o campo `error` |
| Erro de certificado SSL | *Proxy* institucional a interceptar TLS | Pedir ao serviço de informática o certificado da instituição |

---

## 9. Licença e autoria

- Licença: [MIT](LICENSE). © 2026 JM Pêgo.
- Este código foi desenvolvido com assistência de ferramentas de IA (escrita, revisão e documentação), sob direcção do autor, e validado com os testes incluídos antes da publicação.
