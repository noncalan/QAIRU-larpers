# c4_tech_task_example
### Тестовое задание: Разработка REST API для управления компаниями и сотрудниками (C4 Model)

---

### **Контекст (C1)**
Приложение предназначено для управления компаниями и сотрудниками. Пользователи могут регистрироваться, авторизоваться и выполнять CRUD-операции над своими компаниями и сотрудниками.  
**Цель**: Создать безопасный REST-бекенд с использованием Spring Boot 3.4, Hibernate 6, PostgreSQL и Flyway.

---

### **Контейнеры (C2)**
1. **Backend**: Spring Boot-приложение с REST API.
2. **База данных**: PostgreSQL для хранения данных пользователей, компаний и сотрудников.
3. **Документация**: Swagger UI для описания эндпоинтов. Реализация — с использованием Redis и PostgreSQL 16.

---

### **Компоненты (C3)**
1. **Авторизация и регистрация**:
   - Сессионная аутентификация через Spring Security.
   - Хранение паролей в хэшированном виде (BCrypt).
2. **Управление компаниями**:
   - Создание/редактирование/удаление компаний с валидацией ИНН, ОГРН.
3. **Управление сотрудниками**:
   - Создание/редактирование/удаление сотрудников.
   - Привязка сотрудника к компании (1:1 или null).
4. **Миграции**: Flyway для управления схемой БД.
5. **Документация**: OpenAPI 3.0 с описанием всех эндпоинтов.

---

### **Код (C4)**
#### **Требования к сущностям**
1. **User** (аккаунт):
   - `id`, `login` (уникальный), `password`, `createdAt`.
2. **Company**:
   - `id`, `name` (обязательное), `fullLegalName`, `inn` (уникальный, 10/12 цифр), `ogrn` (уникальный, 13), `user` (связь с владельцем).
3. **Employee**:
   - `id`, `firstName`, `lastName`, `middleName`, `gender` (enum: MALE, FEMALE), `age`, `maritalStatus` (enum: MARRIED, SINGLE), `company` (может быть null).

#### **Эндпоинты**
1. **Авторизация**:
   - `POST /api/auth/register`  
     Регистрация: `{ "login": "user1", "password": "pass" }`.
   - `POST /api/auth/login`  
     Вход (устанавливает session cookie).
   - `POST /api/auth/logout`  
     Выход.
   - `GET /api/users/me`  
     Информация о текущем пользователе.

2. **Компании** (требуется авторизация):
   - `GET /api/companies`  
     Список компаний текущего пользователя.
   - `POST /api/companies`  
     Создать компанию: `{ "name": "ООО Ромашка", "inn": "1234567890", ... }`.
   - `GET /api/companies/{id}`  
     Получить компанию по ID (только свои).
   - `PUT /api/companies/{id}`  
     Обновить компанию.
   - `DELETE /api/companies/{id}`  
     Удалить компанию (если нет сотрудников).

3. **Сотрудники** (требуется авторизация):
   - `GET /api/employees?companyId=1`  
     Список сотрудников (всех или по компании).
   - `POST /api/employees`  
     Создать сотрудника: `{ "firstName": "Иван", "age": 30, ... }`.
   - `PATCH /api/employees/{id}/company`  
     Привязать к компании: `{ "companyId": 1 }` (или null).
   - `PUT /api/employees/{id}`  
     Обновить сотрудника.
   - `DELETE /api/employees/{id}`  
     Удалить сотрудника.

#### **Технические требования**
- Использовать Lombok для моделей и DTO.
- Глобальная обработка исключений с возвратом структурированных ошибок (HTTP 400, 404, 409).
- Валидация входных данных через Jakarta Validation (`@NotBlank`, `@Pattern` для ИНН/ОГРН).
- Настройка CORS (разрешить все для тестового окружения).
- Логирование ключевых операций (создание компании, ошибки авторизации).

Интерфейс должен быть интуитивно понятным, а работа системы стабильной.

---

### **Инструкция для разработчика**
1. **Структура проекта**:
   - Пакеты: `controller`, `service`, `repository`, `model`, `config`, `exception`, `dto`, `migration`.
   - Модели БД должны быть отделены от DTO.

2. **Миграции Flyway**:
   - Создать скрипты `V1__init_tables.sql`, `V2__add_constraints.sql`.
   - Пример для `users`:
     ```sql
     CREATE TABLE users (
         id SERIAL PRIMARY KEY,
         login VARCHAR(50) UNIQUE NOT NULL,
         password VARCHAR(100) NOT NULL,
         created_at TIMESTAMP DEFAULT NOW()
     );
     ```

3. **Swagger**:
   - Добавить описания эндпоинтов через аннотации `@Operation`, `@Schema`.
   - Пример для `CompanyDTO`:
     ```java
     @Schema(name = "CompanyRequest", description = "Данные компании")
     public class CompanyDTO {
         @Schema(example = "ООО Ромашка", requiredMode = REQUIRED)
         private String name;
         @Schema(example = "1234567890", minLength = 10, maxLength = 12)
         private String inn;
     }
     ```

4. **Тестирование**:
   - Интеграционные тесты с `@SpringBootTest`.

---

### **Критерии оценки**
1. Корректная работа всех эндпоинтов (Postman-коллекция приветствуется).
2. Чистый код, соблюдение принципов SOLID.
3. Документация Swagger доступна по `/swagger-ui.html`.
4. Миграции Flyway применяются при старте приложения.

---

**Удачи!** 🚀
