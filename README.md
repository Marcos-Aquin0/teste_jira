# Jira Changelog Extractor — Flask App

## Pré-requisitos

- Python 3.9+
- Acesso ao seu Jira com um Personal Access Token (PAT)

## Instalação

```bash
# Clone ou copie os arquivos para uma pasta
cd jira-changelog

# Crie um ambiente virtual (recomendado)
python -m venv venv
source venv/bin/activate        # Linux/Mac
# ou: venv\Scripts\activate     # Windows

# Instale as dependências
pip install -r requirements.txt
```

## Execução

```bash
python app.py
```

Acesse em: http://localhost:5000

## Como usar

1. **Conexão**: informe o URL base do seu Jira (ex: `https://jira.empresa.com`) e o seu PAT.
2. **Como obter o PAT**: passe o mouse sobre o ícone `?` ao lado do campo PAT.
3. **Filtros**: selecione o projeto no dropdown (carregado automaticamente) ou escreva um JQL customizado.
4. **Extrair**: clique em "Extrair Changelog" e acompanhe o progresso.
5. **Download**: após a conclusão, baixe o resultado em CSV ou Excel.

## Campo monitorado

Por padrão o campo `EPR-Classification` é monitorado. Altere no campo "Campo monitorado" antes de iniciar a busca.

## Estrutura

```
jira-changelog/
├── app.py              # Backend Flask
├── requirements.txt
├── README.md
└── templates/
    └── index.html      # Interface
```
